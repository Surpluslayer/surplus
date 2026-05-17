"""
routes/admin.py — cron / operator-triggered tasks.

    POST /admin/run-followups   shared-secret auth (X-Admin-Token)

Idempotent enough to hit from an external cron (Railway, GitHub Actions)
on a regular schedule. Picks prospects that:
  - have a `message_sent` outreach row (the first post-accept DM landed)
  - have not received a `message_replied` since
  - have fewer than FOLLOWUP_MAX_PER_PROSPECT `follow_up_sent` rows
  - last `message_sent` is older than FOLLOWUP_DELAY_HOURS

For each, composes a follow-up and sends via the prospect's owning user's
LinkedIn account (same per-user routing the webhook auto-DM uses).
"""
from __future__ import annotations
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import config, models
from ..agents.outreach import compose_followup
from ..db import get_db
from ..providers import (
    LinkedInProvider,
    get_provider,
    get_provider_for_user,
)


router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """Constant-time compare the X-Admin-Token header against ADMIN_TOKEN env.

    Returns 404 (not 401/403) on missing-or-wrong, matching the demo route's
    no-fingerprinting posture — an attacker scanning shouldn't learn this
    endpoint exists.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(404, "Not Found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(404, "Not Found")


def _aware(dt: datetime) -> datetime:
    """Postgres returns naive datetimes; coerce to UTC-aware for comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _eligible_prospects(db: Session) -> list[models.Prospect]:
    """Find every prospect that's due for a follow-up right now.

    Walks each prospect's outreach log once rather than building a fancy SQL
    aggregate — there are at most a few thousand active prospects per event
    and the JOIN+groupby version would be harder to reason about given the
    legacy email-flavored states still in the table.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FOLLOWUP_DELAY_HOURS
    )

    rows: list[models.Prospect] = []
    candidates = db.query(models.Prospect).filter(
        models.Prospect.status == "contacted"
    ).all()

    for p in candidates:
        if not p.outreach:
            continue
        last_message_sent_ts: Optional[datetime] = None
        replied = False
        followup_count = 0
        for o in p.outreach:
            if o.state == "message_sent":
                ts = _aware(o.ts)
                if last_message_sent_ts is None or ts > last_message_sent_ts:
                    last_message_sent_ts = ts
            elif o.state == "message_replied":
                replied = True
            elif o.state == "follow_up_sent":
                followup_count += 1

        if replied:
            continue
        if followup_count >= config.FOLLOWUP_MAX_PER_PROSPECT:
            continue
        if last_message_sent_ts is None:
            continue
        if last_message_sent_ts > cutoff:
            continue
        rows.append(p)
    return rows


def _provider_for_prospect(
    prospect: models.Prospect,
    fallback: LinkedInProvider,
) -> LinkedInProvider:
    """Route the send through the owning user's LinkedIn account. Mirrors the
    logic in routes/webhooks.py:_provider_for_prospect — kept inline (small
    function, two callsites) rather than extracted to avoid a circular import."""
    event = prospect.event
    if event and event.user_id:
        owner = event.user
        if owner and owner.unipile_account_id:
            try:
                return get_provider_for_user(owner)
            except Exception:
                pass
    return fallback


@router.post("/run-followups", status_code=200)
def run_followups(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Send a follow-up DM to every prospect currently due for one.

    Designed for hourly cron — running it more often is harmless (the
    eligibility window won't shift inside an hour and follow-up rows would
    just exceed FOLLOWUP_MAX_PER_PROSPECT on the second run).
    """
    fallback_provider = get_provider()
    eligible = _eligible_prospects(db)

    sent: list[dict] = []
    failed: list[dict] = []
    now = datetime.now(timezone.utc)

    for prospect in eligible:
        event = prospect.event
        if event is None:
            failed.append({"prospect_id": prospect.id, "error": "no event"})
            continue

        provider = _provider_for_prospect(prospect, fallback_provider)

        # Find the linkedin_provider_id — same fallback chain webhook uses.
        li_provider_id = prospect.linkedin_provider_id
        if not li_provider_id:
            for o in sorted(prospect.outreach, key=lambda o: o.ts, reverse=True):
                if o.state in ("invite_sent", "dry_run_queued"):
                    li_provider_id = o.provider_lead_id
                    break

        peers = [
            p.name for p in event.prospects
            if p.id != prospect.id and p.status in ("approved", "contacted", "rsvp")
        ]
        followup_text = compose_followup(prospect, event, peers=peers)

        # Reuse build_lead_payload — note=followup_text fills the "short msg"
        # slot too (we never read .note here); message is what send_message
        # actually serializes into the Unipile chat POST.
        lead = provider.build_lead_payload(
            prospect, event, note=followup_text, message=followup_text
        )
        try:
            res = provider.send_message(lead, linkedin_provider_id=li_provider_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"prospect_id": prospect.id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        if res.error:
            failed.append({"prospect_id": prospect.id, "error": res.error})
            continue

        db.add(models.OutreachLog(
            prospect_id=prospect.id,
            channel="linkedin",
            state="follow_up_sent",
            body=json.dumps(res.payload, default=str)[:8000],
            ts=now,
            provider=res.provider,
            provider_lead_id=res.provider_lead_id,
        ))
        sent.append({
            "prospect_id": prospect.id,
            "state": res.state,
            "dry_run": res.dry_run,
        })

    if sent:
        db.commit()

    return {
        "eligible": len(eligible),
        "sent": len(sent),
        "failed": len(failed),
        "delay_hours": config.FOLLOWUP_DELAY_HOURS,
        "max_per_prospect": config.FOLLOWUP_MAX_PER_PROSPECT,
        "results": sent,
        "errors": failed,
    }
