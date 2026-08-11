"""backend/referral.py : the referral flywheel.

One home for the whole lifecycle so the six signup sites and the billing webhook
call INTO this, never re-implement it (the same discipline the attribution
design calls for: capture a string at each site, resolve the reward centrally).

The two triggers are kept separate:

  * ASK    : stamp_first_win() marks the moment a paying user first sees value
             (an inbound reply / a booking); should_prompt() is the deterministic
             gate the app reads to show the in-product referral card, exactly once.
  * PAYOUT : on_referee_paid() fires from the Stripe webhook when a referred user
             subscribes. It runs the fraud gates and opens a Referral in `qualified`
             with a clawback HOLD. vest_due_referrals() (a sweep) grants the
             referrer's free month once the hold clears with no refund;
             claw_back_for_referee() rejects it if a refund/cancel lands first.

The reward is realized as `comp_until` on the User row (a free month of Pro,
read by billing_plans) so nothing here mutates Stripe. Two-sided: the referee
also gets a welcome comp month at qualification.
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import models

# A month, for comp math. Kept simple + explicit (matches billing_plans' free
# window granularity); not calendar-aware, which is fine for a comp grant.
_MONTH = timedelta(days=30)

# Code alphabet: lowercase + digits, minus look-alikes (0/o/1/l) so a code is
# safe to say out loud and paste. 8 chars ~ 40 bits, ample for the space.
_ALPHABET = "".join(c for c in (string.ascii_lowercase + string.digits)
                    if c not in "01lo")
_CODE_LEN = 8


# ─── config (env-driven, sane defaults) ──────────────────────────────────────

def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Master switch. Off = capture/qualify/vest all no-op and the prompt never
    shows. Lets the whole flywheel be dark-shipped or killed from one env var."""
    return _flag("REFERRAL_ENABLED", True)


def _reward_months() -> int:
    try:
        return max(1, int(os.environ.get("REFERRAL_REWARD_MONTHS") or 1))
    except ValueError:
        return 1


def _hold_days() -> int:
    """Clawback window. Default 30d ~ the referee's first paid month: the
    referrer vests once the referee has stuck for a month (the design's
    'the free month IS the hold')."""
    try:
        return max(0, int(os.environ.get("REFERRAL_HOLD_DAYS") or 30))
    except ValueError:
        return 30


def _max_rewards_per_year() -> int:
    """Allowance cap: most vested rewards a single referrer can earn per rolling
    year. Blunt anti-farming backstop (Robinhood caps the dollar value; we cap
    the count)."""
    try:
        return max(1, int(os.environ.get("REFERRAL_MAX_REWARDS_PER_YEAR") or 12))
    except ValueError:
        return 12


# ─── helpers ─────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo on round-trip; treat naive as UTC so comparisons and
    arithmetic never raise."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_demo(user) -> bool:
    return bool(getattr(user, "is_demo", False))


def _norm_email(user) -> str:
    return (getattr(user, "email", "") or "").strip().lower()


def _gen_code(db: Session) -> str:
    """A fresh code not already taken. Retries on the (astronomically unlikely)
    collision so the unique constraint never surfaces to a caller."""
    for _ in range(12):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
        exists = db.query(models.User.id).filter(
            models.User.referral_code == code).first()
        if not exists:
            return code
    # Fall back to a longer code rather than loop forever.
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN + 4))


# ─── referral code (the share link) ──────────────────────────────────────────

def ensure_referral_code(db: Session, user) -> Optional[str]:
    """This user's stable share code, minting it on first use. Idempotent."""
    existing = getattr(user, "referral_code", None)
    if existing:
        return existing
    code = _gen_code(db)
    user.referral_code = code
    db.add(user)
    db.commit()
    return code


def user_for_code(db: Session, code: Optional[str]) -> Optional[models.User]:
    """The referrer who owns `code`, if any (case-insensitive)."""
    if not code:
        return None
    code = code.strip().lower()
    if not code:
        return None
    return (db.query(models.User)
            .filter(models.User.referral_code == code,
                    models.User.is_demo.is_(False))
            .first())


# ─── attribution capture (at signup) ─────────────────────────────────────────

def capture_referral_code(db: Session, new_user, raw_code: Optional[str]) -> bool:
    """Bind `raw_code` (from the surplus_ref cookie) onto a freshly created user.

    Called once per signup, from the single account-creation seam. Fail-soft and
    idempotent: refuses to overwrite an existing attribution, ignores an unknown
    code, and never binds a user to their own code (self-referral). Returns True
    iff an attribution was recorded. Never raises into the signup path."""
    if not enabled() or new_user is None:
        return False
    try:
        if getattr(new_user, "referred_by_code", None):
            return False  # already attributed; first touch wins
        if _is_demo(new_user):
            return False
        code = (raw_code or "").strip().lower()
        if not code:
            return False
        referrer = user_for_code(db, code)
        if referrer is None:
            return False
        if referrer.id == getattr(new_user, "id", None):
            return False  # self-referral
        new_user.referred_by_code = code
        db.add(new_user)
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 : attribution must never break signup
        print(f"  [referral] capture failed (non-fatal): {exc}")
        try:
            db.rollback()
        except Exception:
            pass
        return False


# ─── ASK: first-win stamp + prompt gate ──────────────────────────────────────

def stamp_first_win(db: Session, user, *, kind: str = "reply") -> bool:
    """Record the first real win for a PAYING, non-demo user, once. `kind` is
    for logging only ('reply' | 'booking'). Returns True iff it stamped now.

    Called from the webhook path AFTER the (tenant-scoped, post-H3) prospect
    resolution, so the win is attributed to the correct owner."""
    if not enabled() or user is None:
        return False
    try:
        if getattr(user, "first_win_at", None) is not None:
            return False
        if _is_demo(user):
            return False
        from . import billing_plans
        if not billing_plans.is_paid(user):
            return False
        user.first_win_at = _utcnow()
        db.add(user)
        db.commit()
        print(f"  [referral] first_win stamped user={user.id} kind={kind}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [referral] stamp_first_win failed (non-fatal): {exc}")
        try:
            db.rollback()
        except Exception:
            pass
        return False


def should_prompt(user) -> bool:
    """Deterministic gate for the in-product referral card. Pure read of the
    user row: paying, has had a first win, not yet prompted, not demo."""
    if not enabled() or user is None:
        return False
    if _is_demo(user):
        return False
    if getattr(user, "first_win_at", None) is None:
        return False
    if getattr(user, "referral_prompted_at", None) is not None:
        return False
    from . import billing_plans
    return billing_plans.is_paid(user)


def mark_prompted(db: Session, user) -> None:
    """Stamp that the ask fired, so should_prompt() goes False forever after."""
    if user is None or getattr(user, "referral_prompted_at", None) is not None:
        return
    user.referral_prompted_at = _utcnow()
    db.add(user)
    db.commit()


# ─── PAYOUT: qualify → hold → vest, with clawback ────────────────────────────

def _fraud_reason(db: Session, referrer, referee) -> Optional[str]:
    """The gate a (referrer, referee) pair fails, or None if it passes. Ordered
    cheapest / most-decisive first."""
    if referrer is None:
        return "no_referrer"
    if referrer.id == referee.id:
        return "self_referral"
    if _is_demo(referrer) or _is_demo(referee):
        return "demo"
    if not getattr(referee, "email_verified", False):
        return "unverified"
    r_email, e_email = _norm_email(referrer), _norm_email(referee)
    if r_email and r_email == e_email:
        return "self_referral"
    r_li = getattr(referrer, "linkedin_provider_id", None)
    e_li = getattr(referee, "linkedin_provider_id", None)
    if r_li and e_li and r_li == e_li:
        return "self_referral"
    if _rewards_in_last_year(db, referrer.id) >= _max_rewards_per_year():
        return "allowance_cap"
    return None


def _rewards_in_last_year(db: Session, referrer_id: int) -> int:
    since = _utcnow() - timedelta(days=365)
    return (db.query(models.Referral)
            .filter(models.Referral.referrer_id == referrer_id,
                    models.Referral.status == "rewarded",
                    models.Referral.rewarded_at >= since)
            .count())


def grant_comp_months(user, months: int, *, now: Optional[datetime] = None) -> None:
    """Extend the user's comp window by `months`, stacking from the later of now
    or their current comp_until (so queued months don't overlap and get lost).
    Caller commits."""
    now = now or _utcnow()
    cur = _aware(getattr(user, "comp_until", None))
    base = cur if (cur is not None and cur > now) else now
    user.comp_until = base + _MONTH * max(1, int(months))


def on_referee_paid(db: Session, referee) -> Optional[models.Referral]:
    """Referred user just paid: open the referral in `qualified` under a hold,
    and grant the referee their welcome comp month (two-sided). Idempotent on
    referee_id. Returns the Referral row, or None if there's nothing to do.

    Does NOT grant the referrer yet: that waits for the hold to clear (see
    vest_due_referrals), so a fast refund/cancel claws it back for free."""
    if not enabled() or referee is None:
        return None
    code = (getattr(referee, "referred_by_code", None) or "").strip().lower()
    if not code:
        return None
    existing = (db.query(models.Referral)
                .filter(models.Referral.referee_id == referee.id).first())
    if existing is not None:
        return existing  # already processed this referee's conversion

    referrer = user_for_code(db, code)
    now = _utcnow()
    reason = _fraud_reason(db, referrer, referee) if referrer else "no_referrer"
    if reason is not None:
        # Record the rejected attempt for auditability, but only when we have a
        # referrer to hang it on (referee_id is unique, so this also blocks
        # retries from re-qualifying a rejected referee).
        if referrer is not None:
            ref = models.Referral(
                referrer_id=referrer.id, referee_id=referee.id, code=code,
                status="rejected", reason=reason, created_at=now)
            db.add(ref)
            db.commit()
            print(f"  [referral] rejected referee={referee.id} reason={reason}")
            return ref
        return None

    ref = models.Referral(
        referrer_id=referrer.id, referee_id=referee.id, code=code,
        status="qualified", qualified_at=now,
        hold_until=now + timedelta(days=_hold_days()), created_at=now)
    db.add(ref)
    # Two-sided: the referee's welcome free month, granted now.
    grant_comp_months(referee, _reward_months(), now=now)
    db.add(referee)
    db.commit()
    print(f"  [referral] qualified referee={referee.id} referrer={referrer.id} "
          f"hold_until={ref.hold_until.isoformat()}")
    return ref


def vest_due_referrals(db: Session, *, now: Optional[datetime] = None,
                       limit: int = 200) -> int:
    """Grant the referrer's free month for every referral whose hold has cleared
    with no clawback. Idempotent + safe to run on a schedule; returns the count
    vested this pass."""
    if not enabled():
        return 0
    now = now or _utcnow()
    due = (db.query(models.Referral)
           .filter(models.Referral.status == "qualified",
                   models.Referral.hold_until <= now)
           .limit(limit)
           .all())
    vested = 0
    for ref in due:
        referrer = db.get(models.User, ref.referrer_id)
        if referrer is None:
            ref.status = "rejected"
            ref.reason = "referrer_gone"
            continue
        # Re-check the allowance cap at vest time (a burst could have qualified
        # many at once); over-cap rewards are rejected, not granted.
        if _rewards_in_last_year(db, referrer.id) >= _max_rewards_per_year():
            ref.status = "rejected"
            ref.reason = "allowance_cap"
            continue
        grant_comp_months(referrer, _reward_months(), now=now)
        ref.status = "rewarded"
        ref.rewarded_at = now
        db.add(referrer)
        vested += 1
    db.commit()
    if vested:
        print(f"  [referral] vested {vested} referral reward(s)")
    return vested


def claw_back_for_referee(db: Session, referee, *, reason: str = "refund") -> int:
    """A referred user refunded / cancelled. Reject any still-HELD referral so
    the referrer's reward never lands. A reward that already vested is left
    alone (the free month was earned by a month of real subscription). Returns
    the number of referrals clawed back."""
    if referee is None:
        return 0
    held = (db.query(models.Referral)
            .filter(models.Referral.referee_id == referee.id,
                    models.Referral.status == "qualified")
            .all())
    for ref in held:
        ref.status = "rejected"
        ref.reason = reason
    if held:
        db.commit()
        print(f"  [referral] clawed back {len(held)} for referee={referee.id} "
              f"reason={reason}")
    return len(held)


# ─── reads for the UI ────────────────────────────────────────────────────────

def referrals_for(db: Session, referrer_id: int) -> list[models.Referral]:
    return (db.query(models.Referral)
            .filter(models.Referral.referrer_id == referrer_id)
            .order_by(models.Referral.created_at.desc())
            .all())


def months_banked(db: Session, referrer_id: int) -> int:
    return (db.query(models.Referral)
            .filter(models.Referral.referrer_id == referrer_id,
                    models.Referral.status == "rewarded")
            .count()) * _reward_months()
