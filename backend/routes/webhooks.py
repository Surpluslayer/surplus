"""
routes/webhooks.py : provider webhook ingestion.

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

from datetime import datetime, timezone

from .. import models
from ..db import get_db
from ..agents import relationships
from ..agents.outreach import compose
from ..agents.reply_agent import (
    ReplyDecision, ThreadMessage, decide_reply, should_auto_send,
)
from ..agents.sender import send_and_log
from ..providers import (
    get_provider,
    get_provider_for_prospect,
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
    Idempotent : dedup by (prospect_id, state, provider, provider_lead_id).
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

    # invite_accepted means the recipient is now a 1st-degree connection,
    # so future "reach out" actions on this prospect should take the warm
    # path. Stamp connection_status here so we don't need another Unipile
    # round-trip the next time the UI loads.
    if ev.state == "invite_accepted":
        prospect.connection_status = "connected"
        prospect.connection_checked_at = datetime.now(timezone.utc)

    # Spine: every applied funnel event is a real touch, so ensure this person
    # exists as a durable Contact (idempotent, fail-soft, no-op without a strong
    # identity key). This is the "auto-populate once they connect" hook : an
    # invite_accepted lands here and the person enters the cross-event graph.
    owner_id = getattr(getattr(prospect, "event", None), "user_id", None)
    if owner_id is not None:
        relationships.link_contact(db, prospect, owner_id)

    db.commit()
    return True, "applied", prospect


def _trigger_auto_dm(
    db: Session,
    provider: LinkedInProvider,
    prospect: models.Prospect,
) -> Optional[dict]:
    """For providers where the platform owns the sequence (Unipile), fire
    the post-accept DM ourselves : from the OWNING USER'S LinkedIn."""
    if not provider.auto_dm_after_accept:
        return None

    event = prospect.event
    peers = [p.name for p in event.prospects if p.id != prospect.id and
             p.status in ("approved", "contacted", "rsvp")]
    msg = compose(prospect, event, peers=peers)
    res = send_and_log(
        db, prospect, msg.message,
        sent_state="message_sent", fallback_provider=provider,
    )
    if not res.dry_run and res.state == "message_sent":
        # Auto-stage the follow-up the instant the post-accept DM lands, so the
        # host has a reviewable scheduled nudge without lifting a finger.
        from ..agents.followup_scheduler import stage_followup
        stage_followup(db, prospect, commit=True)
    return {"state": res.state, "dry_run": res.dry_run, "error": res.error}


@router.post("/unipile", status_code=200)
async def unipile_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    provider = get_provider()
    if provider.name != "unipile":
        raise HTTPException(400, f"provider mismatch (configured: {provider.name})")
    return await _handle(request, db, provider)


def _apply_account_status(db: Session, provider, info: dict) -> dict:
    """Circuit breaker. Map a Unipile account-status push onto the host's
    linkedin_status. OK -> 'active'; anything else halts the account -- the auth
    gate AND the updates poller both require linkedin_status == 'active', so the
    moment LinkedIn pushes back (creds / captcha / checkpoint / restriction) we
    stop sending and polling for that host. Auto-resumes when OK arrives."""
    from .. import models
    acct, status = info["account_id"], info["status"]
    if status in getattr(provider, "ACCOUNT_OK", set()):
        new = "active"
    elif status == "CREDENTIALS":
        new = "credentials"
    elif status in ("DISCONNECTED", "STOPPED", "ERROR"):
        new = "disconnected"
    else:                                   # captcha / checkpoint / permissions / blocked
        new = "restricted"
    user = db.query(models.User).filter(models.User.unipile_account_id == acct).first()
    if user is None:
        print(f"[unipile] account_status for unknown acct ...{acct[-4:]} "
              f"status={status}", flush=True)
        return {"ok": True, "applied": False, "account_status": status,
                "reason": "unknown account"}
    prev = user.linkedin_status
    user.linkedin_status = new
    db.commit()
    halted = new != "active"
    print(f"[unipile] {'>>> ACCOUNT HALT' if halted else 'account ok'} "
          f"user={user.id} acct=...{acct[-4:]} status={status} "
          f"linkedin_status {prev}->{new}", flush=True)
    return {"ok": True, "applied": True, "account_status": status,
            "halted": halted, "user_id": user.id}


async def _handle(request: Request, db: Session, provider: LinkedInProvider) -> dict:
    raw_body = await request.body()
    if not provider.verify_webhook(dict(request.headers), raw_body):
        raise HTTPException(401, "webhook signature verification failed")

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "malformed JSON body")

    # Circuit breaker: an account-status push (credentials / restriction /
    # checkpoint) means LinkedIn is pushing back on this account. Halt ALL
    # activity for that host immediately, before it escalates to a ban.
    acct_status = getattr(provider, "parse_account_status", lambda _: None)(payload)
    if acct_status is not None:
        return _apply_account_status(db, provider, acct_status)

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
        # They engaged : kill any pending scheduled follow-up so we never send
        # a "circling back" to someone who already replied.
        from ..agents.followup_scheduler import cancel_pending_followups
        cancel_pending_followups(db, prospect.id, reason="replied")
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
    message_sent log row : that's where send_message stamped it."""
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
    print(f"  [ai_reply] message_replied prospect_id={prospect.id} "
          f"body={canonical.body[:100]!r}")
    event = prospect.event
    if event is None:
        print(f"  [ai_reply] SKIP prospect_id={prospect.id} : no event linked")
        return None

    chat_id = _last_chat_id(prospect)
    thread_raw = provider.fetch_thread(chat_id) if chat_id else []
    if canonical.body:
        thread_raw = list(thread_raw) + [
            {"direction": "inbound", "text": canonical.body, "ts": ""}
        ]
    thread = [ThreadMessage(direction=m["direction"], text=m["text"], ts=m.get("ts"))
              for m in thread_raw if m.get("text")]
    print(f"  [ai_reply] thread fetched: chat_id={chat_id} "
          f"messages={len(thread)} (calling Claude...)")

    host = event.user
    # Ground the reply in prior relationship history (outbound-safe : the brief
    # firewalls private notes). None on a fresh contact / on any read error, so
    # the reply path degrades to thread-only context rather than failing.
    try:
        relationship_ctx = relationships.relationship_context(
            prospect, relationships.fetch_interactions(db, prospect))
    except Exception as exc:  # noqa: BLE001
        print(f"  [ai_reply] relationship_context skipped: {type(exc).__name__}: {exc}")
        relationship_ctx = None
    decision = decide_reply(thread, event, prospect, host=host,
                            relationship_ctx=relationship_ctx)
    print(f"  [ai_reply] decision: classification={decision.classification} "
          f"elapsed={decision.elapsed_s}s draft_chars={len(decision.draft_text)} "
          f"error={decision.error}")

    prior_auto = sum(
        1 for o in prospect.outreach if o.state == "auto_reply_sent"
    )

    if should_auto_send(decision, prior_auto):
        print(f"  [ai_reply] gate PASS → auto-sending")
        return _auto_send_reply(db, provider, prospect, decision)
    print(f"  [ai_reply] gate BLOCK → queueing (class={decision.classification} "
          f"prior_auto={prior_auto})")
    return _queue_pending_reply(db, prospect, decision, canonical.body or "")


# Inbound webhook bodies are user-controlled; cap at 5KB so a malicious
# payload can't bloat the table or slow queries.
_INBOUND_BODY_MAX = 5_000


def _auto_send_reply(
    db: Session,
    fallback_provider: LinkedInProvider,
    prospect: models.Prospect,
    decision: ReplyDecision,
) -> dict:
    res = send_and_log(
        db, prospect, decision.draft_text,
        sent_state="auto_reply_sent", fallback_provider=fallback_provider,
    )
    print(f"  [ai_reply] send result: state={res.state} dry_run={res.dry_run} "
          f"provider_lead_id={res.provider_lead_id} error={res.error}")
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
    db.add(models.PendingReply(
        prospect_id=prospect.id,
        inbound_body=inbound_body[:_INBOUND_BODY_MAX],
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


# ── Bright Data delivery : contact updates (job-change + posts) ──────────────
def _norm_li(url: str) -> str:
    """Normalize a LinkedIn profile URL to its /in/<slug> for matching."""
    u = (url or "").strip().lower().rstrip("/")
    if "/in/" in u:
        u = "in/" + u.split("/in/", 1)[1].split("?")[0].split("/")[0]
    return u


def _contacts_by_url(db: Session, url: Optional[str]) -> list:
    """Every Contact (across users) whose linkedin_url matches `url`."""
    key = _norm_li(url or "")
    if not key:
        return []
    rows = (db.query(models.Contact)
            .filter(models.Contact.linkedin_url.isnot(None)).all())
    return [c for c in rows if _norm_li(c.linkedin_url) == key]


@router.post("/brightdata", status_code=200)
async def brightdata_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Receive a Bright Data scrape delivery and turn it into Updates.

    Profile records -> job-change diff; post records -> milestone cascade. Each is
    matched to Contact(s) by linkedin_url and emitted as an activity_update (with
    an auto-drafted follow-up). Always 200 + best-effort so Bright Data never
    enters a retry storm. Falls through harmlessly if Bright Data isn't wired.
    """
    from ..providers import brightdata
    from ..agents import updates_engine

    secret = brightdata.webhook_secret()
    if secret:
        auth = (request.headers.get("authorization")
                or request.headers.get("Authorization") or "").strip()
        if auth != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="bad webhook secret")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "applied": 0, "note": "unparseable body"}

    kind = (request.query_params.get("notify") or "").strip().lower()
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("data") or payload.get("results") or [payload]
    else:
        records = []

    applied = 0
    for rec in records:
        try:
            is_posts = kind == "posts" or (isinstance(rec, dict) and rec.get("posts"))
            if is_posts:
                norm = brightdata.normalize_posts(rec)
                for c in _contacts_by_url(db, norm.get("linkedin_url")):
                    applied += len(updates_engine.apply_posts(db, c, norm.get("posts") or []))
            else:
                norm = brightdata.normalize_profile(rec)
                for c in _contacts_by_url(db, norm.get("linkedin_url")):
                    applied += len(updates_engine.apply_profile(db, c, norm))
        except Exception as exc:  # noqa: BLE001 : one bad record never sinks the delivery
            print(f"  [brightdata] record skipped: {type(exc).__name__}: {exc}", flush=True)
    db.commit()
    return {"ok": True, "applied": applied, "kind": kind or "profile"}
