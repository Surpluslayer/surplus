"""
outreach_pacer.py — LinkedIn ban-prevention safeguards on outreach sends.

Two enforcement layers:

1. Daily send cap (default 20/user/day)
   Even established LinkedIn accounts get restricted at ~100/week. 20/day
   leaves comfortable headroom — a user could hit the cap every day of
   the week and still be at ~140/week, well below LinkedIn's threshold.

2. Random pacing between sends (default 2-10 min)
   Burst behavior is the #1 ban signal even for established accounts.
   Random gaps look human; uniform 30-second gaps look botty.

Both are enforced synchronously at send time. When a user hits a cap,
we return 429 with retry-after metadata so the UI can show "wait Xm".
We don't queue + drain in a background worker — synchronous rejection
is simpler, works at this scale, and gives the user immediate feedback.

Config (env vars, all optional):
  SEND_DAILY_CAP            int, default 20
  SEND_PACE_MIN_SECONDS     int, default 120  (2 min)
  SEND_PACE_MAX_SECONDS     int, default 600  (10 min)
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from . import models


# ─── Config ──────────────────────────────────────────────────────────

def _daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("SEND_DAILY_CAP", "20")))
    except ValueError:
        return 20


def _pace_min_seconds() -> int:
    try:
        return max(0, int(os.environ.get("SEND_PACE_MIN_SECONDS", "120")))
    except ValueError:
        return 120


def _pace_max_seconds() -> int:
    try:
        return max(_pace_min_seconds() + 1, int(os.environ.get("SEND_PACE_MAX_SECONDS", "600")))
    except ValueError:
        return 600


# ─── Utility ─────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns naive datetimes; SQLite returns whatever was stored.
    Coerce both to UTC-aware so comparisons with _utcnow() are safe.
    Same helper pattern as backend/auth.py."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Public types ────────────────────────────────────────────────────

@dataclass
class PacerDecision:
    """Result of check_send_allowed(). One of three states:
      allowed=True               → caller may proceed
      allowed=False, reason=*    → caller should reject with 429 + the
                                   reason string + retry_after_seconds
                                   (None means "tomorrow" for the daily cap)
    """
    allowed: bool
    reason: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    sends_today: int = 0
    daily_cap: int = 0
    next_send_allowed_at: Optional[datetime] = None


# ─── Core API ────────────────────────────────────────────────────────

def _sends_today_count(user: models.User, db: DbSession) -> int:
    """Count rows in OutreachLog that this user sent today (UTC).

    Joins OutreachLog → Prospect → Event → user_id. At small scale this is
    fine; would need an index or denormalization at higher volume.

    Only counts states that represent an actual outbound attempt to
    LinkedIn: invite_sent, message_sent, follow_up_sent, dry_run_queued.
    Excludes failed sends (they didn't consume a quota slot).
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    OUTBOUND_STATES = (
        "invite_sent", "message_sent", "follow_up_sent", "dry_run_queued",
    )
    return (
        db.query(models.OutreachLog)
        .join(models.Prospect, models.OutreachLog.prospect_id == models.Prospect.id)
        .join(models.Event, models.Prospect.event_id == models.Event.id)
        .filter(models.Event.user_id == user.id)
        .filter(models.OutreachLog.state.in_(OUTBOUND_STATES))
        .filter(models.OutreachLog.ts >= today_start)
        .count()
    )


def check_send_allowed(user: models.User, db: DbSession) -> PacerDecision:
    """Should `user` be allowed to send a LinkedIn outreach right now?
    Call from every outreach route BEFORE invoking the provider."""
    cap = _daily_cap()
    now = _utcnow()

    # (1) daily cap
    sends_today = _sends_today_count(user, db)
    if sends_today >= cap:
        return PacerDecision(
            allowed=False,
            reason=f"daily_cap_reached:{cap}",
            retry_after_seconds=None,  # tomorrow
            sends_today=sends_today,
            daily_cap=cap,
        )

    # (2) pacing
    next_allowed = _as_aware_utc(user.pacer_next_send_at)
    if next_allowed and next_allowed > now:
        wait_s = int((next_allowed - now).total_seconds()) + 1
        return PacerDecision(
            allowed=False,
            reason=f"too_soon_since_last_send:{wait_s}s",
            retry_after_seconds=wait_s,
            sends_today=sends_today,
            daily_cap=cap,
            next_send_allowed_at=next_allowed,
        )

    return PacerDecision(
        allowed=True,
        sends_today=sends_today,
        daily_cap=cap,
        next_send_allowed_at=next_allowed,
    )


def record_send(user: models.User, db: DbSession) -> datetime:
    """Mark that `user` just sent. Sets pacer_next_send_at to now +
    random(min, max). Returns the new pacer_next_send_at.
    Call AFTER a successful provider.send_* call."""
    delay = random.randint(_pace_min_seconds(), _pace_max_seconds())
    next_at = _utcnow() + timedelta(seconds=delay)
    user.pacer_next_send_at = next_at
    db.commit()
    return next_at


def quota_snapshot(user: models.User, db: DbSession) -> dict:
    """Returns the user's current quota state for the UI to render.
      { sends_today, daily_cap, next_send_allowed_at, can_send_now }
    """
    decision = check_send_allowed(user, db)
    return {
        "sends_today": decision.sends_today,
        "daily_cap": decision.daily_cap,
        "next_send_allowed_at": (
            decision.next_send_allowed_at.isoformat()
            if decision.next_send_allowed_at else None
        ),
        "can_send_now": decision.allowed,
        "reason": decision.reason,
    }
