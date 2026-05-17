"""
routes/webhooks.py — provider webhook ingestion.

    POST /webhooks/unipile     idempotent, HMAC-verified

Auto-DM trigger: when `provider.auto_dm_after_accept` is True AND the
incoming event is `invite_accepted`, the route immediately calls
`provider.send_message(...)` and records a `message_sent` row.

Idempotency: dedup by (prospect_id, state, provider_lead_id).
Unknown events: 200 + applied=false (never crash, never trigger retry storms).
"""
from __future__ import annotations
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..agents.outreach import compose
from ..agents.reply_agent import (
    ReplyDecision, ThreadMessage, decide_reply, should_auto_send,
)
from ..providers import (
    get_provider,
    get_provider_for_user,
    CanonicalEvent,
    LinkedInProvider,
)


router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Canonical state -> resulting prospect.status (the LinkedIn funnel mapping).
_PROSPECT_STATUS_TRANSITIONS: dict[str, str] = {
    "invite_sent":      "contacted",
    "invite_accepted":  "contacted",
    "message_sent":     "contacted",
    "message_replied":  "rsvp",
    "follow_up_sent":   "contacted",
}


def _resolve_prospect(db: Session, ev: CanonicalEvent) -> Optional[models.Prospect]:
    """
    Resolve a webhook event back to its Prospect row.

    Unipile webhooks don't carry our internal event_id / prospect_id; we look
    up by the linkedin_provider_id we cached at send_connection time.
    """
    if ev.event_id and ev.prospect_id:
        return db.get(models.Prospect, ev.prospect_id)
    if ev.provider_lead_id:
        return db.query(models.Prospect).filter_by(
            linkedin_provider_id=ev.provider_lead_id
        ).first()
    return None


def _apply_canonical_event(
    db: Session,
    provider: LinkedInProvider,
    ev: CanonicalEvent,
) -> tuple[bool, str, Optional[models.Prospect]]:
    """
    Apply a normalized event to the DB. Returns (applied, reason, prospect).
    Idempotent — dedup by (prospect_id, state, provider, provider_lead_id).
    """
    prospect = _resolve_prospect(db, ev)
    if prospect is None:
        return False, "no matching prospect found for this event", None

    if ev.event_id and prospect.event_id != ev.event_id:
        return False, (
            f"event_id mismatch (webhook={ev.event_id}, "
            f"prospect.event_id={prospect.event_id})"
        ), None

    # dedup
    for existing in prospect.outreach:
        if (existing.state == ev.state
                and (existing.provider_lead_id or "") == (ev.provider_lead_id or "")
                and (existing.provider or "") == ev.provider):
            return False, "duplicate event already recorded", prospect

    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="linkedin",
        state=ev.state,
        body=ev.body or "",
        ts=ev.ts,
        provider=ev.provider,
        provider_lead_id=ev.provider_lead_id,
    ))

    new_status = _PROSPECT_STATUS_TRANSITIONS.get(ev.state)
    if new_status and prospect.status != new_status:
        if not (prospect.status == "rsvp" and new_status == "contacted"):
            prospect.status = new_status

    db.commit()
    return True, "applied", prospect


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _provider_for_prospect(
    prospect: models.Prospect,
    fallback: LinkedInProvider,
) -> LinkedInProvider:
    """Resolve which LinkedIn account should send on behalf of this prospect.

    Webhooks have no session cookie (Unipile is server-to-server), so we
    can't ask current_user. Instead we trace ownership through the data:

        Prospect → Event → Event.user → that user's Unipile account_id

    If the chain is intact AND that user has a LinkedIn connected (the
    expected production case), we send from THEIR account. Otherwise we
    fall back to the env-var operator account — this preserves behavior
    for legacy events whose user_id was backfilled to the operator.
    """
    event = prospect.event
    if event and event.user_id:
        owner = event.user  # relationship loads the User
        if owner and owner.unipile_account_id:
            try:
                return get_provider_for_user(owner)
            except Exception:
                # Owner exists but their connection is stale — fall through
                # to the env-var fallback rather than failing the webhook.
                pass
    return fallback


def _trigger_auto_dm(
    db: Session,
    provider: LinkedInProvider,
    prospect: models.Prospect,
) -> Optional[dict]:
    """
    For providers where the platform owns the sequence (Unipile), fire the
    post-accept DM ourselves — from the OWNING USER'S LinkedIn, not the
    env-var operator account.
    """
    if not provider.auto_dm_after_accept:
        return None

    # Per-user routing: send from the user who owns this prospect's event.
    routed_provider = _provider_for_prospect(prospect, fallback=provider)

    li_provider_id = prospect.linkedin_provider_id
    if not li_provider_id:
        for o in sorted(prospect.outreach, key=lambda o: o.ts, reverse=True):
            if o.state in ("invite_sent", "dry_run_queued"):
                li_provider_id = o.provider_lead_id
                break

    event = prospect.event
    peers = [p.name for p in event.prospects if p.id != prospect.id and
             p.status in ("approved", "contacted", "rsvp")]
    msg = compose(prospect, event, peers=peers)
    lead = routed_provider.build_lead_payload(
        prospect, event, note=msg.note, message=msg.message
    )
    res = routed_provider.send_message(lead, linkedin_provider_id=li_provider_id)

    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="linkedin",
        state=res.state,
        body=json.dumps(res.payload, default=str)[:8000],
        ts=_now(),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    db.commit()
    return {"state": res.state, "dry_run": res.dry_run, "error": res.error}


@router.post("/unipile", status_code=200)
async def unipile_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    provider = get_provider()
    if provider.name != "unipile":
        raise HTTPException(400, f"provider mismatch (configured: {provider.name})")
    return await _handle(request, db, provider)


async def _handle(request: Request, db: Session, provider: LinkedInProvider) -> dict:
    raw_body = await request.body()
    if not provider.verify_webhook(dict(request.headers), raw_body):
        raise HTTPException(401, "webhook signature verification failed")

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "malformed JSON body")

    canonical = provider.normalize_webhook(payload)
    if canonical is None:
        return {"ok": True, "applied": False,
                "reason": "unhandled event type or missing back-pointers"}

    applied, reason, prospect = _apply_canonical_event(db, provider, canonical)

    auto_dm = None
    if applied and prospect is not None and canonical.state == "invite_accepted":
        auto_dm = _trigger_auto_dm(db, provider, prospect)

    ai_reply = None
    if applied and prospect is not None and canonical.state == "message_replied":
        ai_reply = _handle_ai_reply(db, provider, prospect, canonical)

    return {
        "ok": True,
        "applied": applied,
        "reason": reason,
        "state": canonical.state,
        "prospect_id": prospect.id if prospect else None,
        "event_id": prospect.event_id if prospect else None,
        "auto_dm": auto_dm,
        "ai_reply": ai_reply,
    }


def _last_chat_id(prospect: models.Prospect) -> Optional[str]:
    """Find the provider's chat/conversation id from the most recent
    message_sent log row — that's where send_message stamped it."""
    for o in sorted(prospect.outreach, key=lambda o: o.ts, reverse=True):
        if o.state == "message_sent" and o.provider_lead_id:
            return o.provider_lead_id
    return None


def _handle_ai_reply(
    db: Session,
    provider: LinkedInProvider,
    prospect: models.Prospect,
    canonical: CanonicalEvent,
) -> Optional[dict]:
    """Run the AI reply agent on an inbound message.

    Flow:
      1. Fetch full thread from provider (dry-run returns a fixture)
      2. Ask the agent to classify + draft
      3. If classification is auto-sendable AND loop guard allows → send now
      4. Otherwise → write a PendingReply row for operator approval

    Returns a small dict for the webhook response body, or None if the
    feature was skipped (e.g. provider has no fetch_thread).
    """
    event = prospect.event
    if event is None:
        return None

    chat_id = _last_chat_id(prospect)
    thread_raw = provider.fetch_thread(chat_id) if chat_id else []
    # Always append the canonical event body — Unipile's fetch_thread may
    # not have indexed the new message yet (eventual consistency).
    if canonical.body:
        thread_raw = list(thread_raw) + [
            {"direction": "inbound", "text": canonical.body, "ts": ""}
        ]
    thread = [ThreadMessage(direction=m["direction"], text=m["text"], ts=m.get("ts"))
              for m in thread_raw if m.get("text")]

    host = event.user
    decision = decide_reply(thread, event, prospect, host=host)

    prior_auto = sum(
        1 for o in prospect.outreach if o.state == "auto_reply_sent"
    )

    if should_auto_send(decision, prior_auto):
        return _auto_send_reply(db, provider, prospect, decision)
    return _queue_pending_reply(db, prospect, decision, canonical.body or "")


def _auto_send_reply(
    db: Session,
    fallback_provider: LinkedInProvider,
    prospect: models.Prospect,
    decision: ReplyDecision,
) -> dict:
    """Send the agent's draft via the owning user's LinkedIn account."""
    routed = _provider_for_prospect(prospect, fallback_provider)
    li_provider_id = prospect.linkedin_provider_id

    event = prospect.event
    lead = routed.build_lead_payload(
        prospect, event, note=decision.draft_text, message=decision.draft_text,
    )
    res = routed.send_message(lead, linkedin_provider_id=li_provider_id)

    db.add(models.OutreachLog(
        prospect_id=prospect.id, channel="linkedin",
        state="auto_reply_sent" if not res.error else "failed",
        body=decision.draft_text[:8000],
        ts=_now(), provider=res.provider, provider_lead_id=res.provider_lead_id,
    ))
    # Also log the agent's decision for the audit trail — separate row so
    # the draft + reasoning live alongside the send result.
    db.add(models.PendingReply(
        prospect_id=prospect.id,
        inbound_body="(see most recent message_replied)",
        classification=decision.classification,
        draft_text=decision.draft_text,
        reasoning=decision.reasoning,
        status="auto_sent" if not res.error else "rejected",
        final_text=decision.draft_text if not res.error else None,
        decided_at=_now(),
    ))
    db.commit()
    return {
        "action": "auto_sent" if not res.error else "send_failed",
        "classification": decision.classification,
        "error": res.error,
    }


def _queue_pending_reply(
    db: Session,
    prospect: models.Prospect,
    decision: ReplyDecision,
    inbound_body: str,
) -> dict:
    """Write a PendingReply row so an operator can approve / edit / reject."""
    db.add(models.PendingReply(
        prospect_id=prospect.id,
        inbound_body=inbound_body,
        classification=decision.classification,
        draft_text=decision.draft_text,
        reasoning=decision.reasoning,
        status="pending",
    ))
    db.commit()
    return {
        "action": "queued",
        "classification": decision.classification,
        "draft_chars": len(decision.draft_text),
    }
