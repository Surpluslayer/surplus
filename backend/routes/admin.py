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
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from .. import config, models
from ..agents.sender import send_and_log
from ..auth import _as_aware_utc
from ..db import get_db
from ..providers import (
    LinkedInProvider,
    get_provider,
    get_provider_for_prospect,
)


class PendingReplyOut(BaseModel):
    id: int
    prospect_id: int
    prospect_name: str
    inbound_body: str
    classification: str
    draft_text: str
    reasoning: str
    status: str
    created_at: datetime


class ApproveBody(BaseModel):
    """Optional edited text — when present, sent instead of the draft."""
    edited_text: Optional[str] = None


class RejectBody(BaseModel):
    reason: Optional[str] = None


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


def _eligible_prospects(db: Session) -> list[models.Prospect]:
    """Find every prospect that's due for a follow-up right now.

    Eager-loads `outreach` so the per-prospect timeline scan doesn't trigger
    one query per row. Legacy email-flavored states (sent/opened/replied)
    coexist with the canonical LinkedIn states here, which is why we walk
    the timeline in Python rather than write a SQL aggregate.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FOLLOWUP_DELAY_HOURS
    )
    candidates = (db.query(models.Prospect)
                    .filter(models.Prospect.status == "contacted")
                    .options(selectinload(models.Prospect.outreach))
                    .all())

    rows: list[models.Prospect] = []
    for p in candidates:
        if not p.outreach:
            continue
        last_message_sent_ts: Optional[datetime] = None
        replied = False
        followup_count = 0
        for o in p.outreach:
            if o.state == "message_sent":
                ts = _as_aware_utc(o.ts)
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
        if last_message_sent_ts is None or last_message_sent_ts > cutoff:
            continue
        rows.append(p)
    return rows


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
    from ..agents.outreach import compose_followup
    fallback_provider = get_provider()
    eligible = _eligible_prospects(db)

    sent: list[dict] = []
    failed: list[dict] = []

    for prospect in eligible:
        event = prospect.event
        if event is None:
            failed.append({"prospect_id": prospect.id, "error": "no event"})
            continue

        text = compose_followup(prospect, event)

        try:
            res = send_and_log(
                db, prospect, text,
                sent_state="follow_up_sent",
                fallback_provider=fallback_provider,
                commit=False,
            )
        except Exception as exc:  # noqa: BLE001
            failed.append({"prospect_id": prospect.id,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue

        if res.error:
            failed.append({"prospect_id": prospect.id, "error": res.error})
            continue

        sent.append({"prospect_id": prospect.id, "state": res.state,
                     "dry_run": res.dry_run})

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


# ── Pending AI replies — list, approve, reject ──────────────────────────

@router.get("/pending-replies", response_model=list[PendingReplyOut])
def list_pending_replies(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Return every PendingReply still awaiting a human decision."""
    rows = (db.query(models.PendingReply)
              .filter(models.PendingReply.status == "pending")
              .order_by(models.PendingReply.created_at.asc())
              .all())
    return [
        PendingReplyOut(
            id=r.id,
            prospect_id=r.prospect_id,
            prospect_name=(r.prospect.name if r.prospect else ""),
            inbound_body=r.inbound_body,
            classification=r.classification,
            draft_text=r.draft_text,
            reasoning=r.reasoning,
            status=r.status,
            created_at=r.created_at,
        ) for r in rows
    ]


def _send_pending(db: Session, pending: models.PendingReply, text: str) -> dict:
    prospect = pending.prospect
    if prospect is None or prospect.event is None:
        raise HTTPException(404, "Not Found")
    res = send_and_log(
        db, prospect, text,
        sent_state="message_sent",
        fallback_provider=get_provider(),
        commit=False,
    )
    pending.status = "approved" if not res.error else "rejected"
    pending.final_text = text if not res.error else None
    pending.decided_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": pending.id, "sent": not bool(res.error),
            "dry_run": res.dry_run, "error": res.error}


@router.post("/pending-replies/{pending_id}/approve")
def approve_pending_reply(
    pending_id: int,
    body: Optional[ApproveBody] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    pending = db.get(models.PendingReply, pending_id)
    if pending is None or pending.status != "pending":
        raise HTTPException(404, "Not Found")
    text = (body.edited_text if body and body.edited_text else pending.draft_text).strip()
    if not text:
        raise HTTPException(400, "empty reply text")
    return _send_pending(db, pending, text)


@router.post("/pending-replies/{pending_id}/reject")
def reject_pending_reply(
    pending_id: int,
    body: Optional[RejectBody] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    pending = db.get(models.PendingReply, pending_id)
    if pending is None or pending.status != "pending":
        raise HTTPException(404, "Not Found")
    pending.status = "rejected"
    pending.decided_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": pending.id, "status": "rejected",
            "reason": (body.reason if body else None)}
