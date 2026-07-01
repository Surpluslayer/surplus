"""routes/messages.py : message capture (context IN) + the send queue (OUT).

Source-agnostic. Any source -- a Unipile webhook, the Mac/Android companion reading
iMessage/SMS, a WhatsApp pull -- POSTs normalized messages here, and they land in the
relationship timeline (which IS the context the drafter reads) keyed by phone/email.

    POST /api/messages/ingest          messages IN -> contact + timeline (+ real phone)
    POST /api/messages/send            queue an outbound message (schedule optional)
    GET  /api/messages/outbox/due      companion polls DEVICE sends that are due
    POST /api/messages/outbox/{id}/sent companion reports a device send done/failed

CLOUD channels (whatsapp/linkedin/email) drain server-side; DEVICE channels
(imessage/sms) drain to the user's companion when a device of theirs is awake.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship.channels import DEVICE_CHANNELS as _DEVICE_CHANNELS
from ..agents.relationship.channels import MESSAGING_CHANNELS as _ALL_CHANNELS
from ..auth import current_user
from ..db import get_db
from ..integrations.sync_common import parse_iso

router = APIRouter(prefix="/api/messages", tags=["messages"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: Optional[str]) -> datetime:
    """Message timestamp -> aware datetime, defaulting to now (a message always gets a
    time). Reuses the tolerant sync_common.parse_iso (handles 'Z', offsets, date-only)."""
    return parse_iso(ts) or _utcnow()


class IncomingMessage(BaseModel):
    handle: str                       # the other person's phone OR email
    name: str = ""
    direction: str = "in"             # "in" (from them) | "out" (from me)
    text: str = ""
    ts: Optional[str] = None          # ISO timestamp
    channel: str = "imessage"
    external_id: Optional[str] = None # source message id, for idempotency


class IngestIn(BaseModel):
    messages: list[IncomingMessage]


class SendIn(BaseModel):
    channel: str
    body: str
    contact_id: Optional[int] = None
    to_handle: Optional[str] = None   # phone/email (required for device sends w/o contact)
    scheduled_at: Optional[str] = None


class SentIn(BaseModel):
    ok: bool = True
    error: str = ""


def _find_or_create_contact(db, user, handle: str, name: str):
    """Find-or-create a Contact by handle (email -> em:, else phone -> ph:). Stores the
    raw phone. Returns (contact, created) or (None, False) if unkeyable."""
    from ..triage.enrichment_cache import identity_keys
    handle = (handle or "").strip()
    is_email = "@" in handle
    keys = identity_keys(email=handle if is_email else "",
                         phone="" if is_email else handle)
    if not keys:
        return None, False
    primary = keys[0]
    contact = (db.query(models.Contact)
               .filter_by(user_id=user.id, primary_identity_key=primary).first())
    created = False
    if contact is None:
        contact = models.Contact(
            user_id=user.id, primary_identity_key=primary, name=name or None,
            email=handle if is_email else None,
            phone=None if is_email else handle)
        db.add(contact); db.flush()
        created = True
    else:
        if is_email and not contact.email:
            contact.email = handle
        if not is_email and not contact.phone:
            contact.phone = handle
        if name and not contact.name:
            contact.name = name
    return contact, created


def ingest_messages(db, user, messages) -> dict:
    """Land a batch of normalized messages in the relationship timeline, keyed
    by phone/email, idempotent per (contact, external_id). The SHARED message
    sink: the HTTP /ingest route, the WhatsApp pull, and any other source funnel
    through here so the spine-upsert + idempotency rules live in ONE place.

    `messages` is an iterable of IncomingMessage (or any object with the same
    fields: handle, name, direction, text, ts, channel, external_id). Commits
    and returns aggregate stats. Channel is carried by source_type (NOT a
    `channel` meta key -- that collides with _item(channel=...) downstream)."""
    stats = {"contacts_created": 0, "appended": 0, "skipped": 0}
    for m in messages:
        contact, created = _find_or_create_contact(db, user, m.handle, m.name)
        if contact is None:
            stats["skipped"] += 1
            continue
        if created:
            stats["contacts_created"] += 1
        # idempotency: skip if we already stored this source message id for this contact
        if m.external_id:
            marker = f'"ext": "{m.external_id}"'
            dup = (db.query(models.RelationshipInteraction)
                   .filter(models.RelationshipInteraction.contact_id == contact.id,
                           models.RelationshipInteraction.meta_json.like(f"%{marker}%"))
                   .first())
            if dup is not None:
                stats["skipped"] += 1
                continue
        # Timeline convention is inbound/outbound (the thread builder maps these to
        # them/host); the API takes the short in/out.
        direction = "outbound" if m.direction == "out" else "inbound"
        db.add(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id,
            source_type=(m.channel or "imessage"), interaction_type="message",
            direction=direction, occurred_at=_parse_ts(m.ts),
            title="", summary=(m.text or "")[:1000],
            # NB: no "channel" key -- it collides with _item(channel=...) in the
            # timeline assembler; the channel is carried by source_type above.
            meta_json=json.dumps({"ext": m.external_id or ""})))
        stats["appended"] += 1
    db.commit()
    return stats


@router.post("/ingest")
def ingest(body: IngestIn, db: Session = Depends(get_db),
           user: models.User = Depends(current_user)):
    """Land incoming messages in the timeline as context, keyed by phone/email.
    Idempotent per (contact, external_id)."""
    return ingest_messages(db, user, body.messages)


@router.post("/send")
def send(body: SendIn, db: Session = Depends(get_db),
         user: models.User = Depends(current_user)):
    """Queue an outbound message (optionally scheduled). Cloud channels drain server-side;
    device channels (imessage/sms) drain to the companion. Returns the queued row."""
    channel = (body.channel or "").strip().lower()
    if channel not in _ALL_CHANNELS:
        raise HTTPException(400, f"unknown channel {channel!r}")
    to_handle = (body.to_handle or "").strip()
    contact = None
    if body.contact_id:
        contact = db.get(models.Contact, body.contact_id)
        if contact is None or contact.user_id != user.id:
            raise HTTPException(404, "contact not found")
        if not to_handle:
            to_handle = (contact.phone or contact.email or "") if channel in _DEVICE_CHANNELS else ""
    if channel in _DEVICE_CHANNELS and not to_handle:
        raise HTTPException(400, "device send needs a phone/email (to_handle or a contact with one)")
    om = models.OutgoingMessage(
        user_id=user.id, contact_id=(contact.id if contact else None),
        channel=channel, to_handle=to_handle or None, body=body.body or "",
        scheduled_at=_parse_ts(body.scheduled_at), status="queued")
    db.add(om); db.commit(); db.refresh(om)

    # CLOUD channels send server-side (no device companion). WhatsApp is a
    # cloud channel on Unipile -- if this send is due now, dispatch it through
    # Unipile and flip the row's status. Scheduled-for-later cloud sends stay
    # queued for the server to drain at scheduled_at. Device channels are NOT
    # touched here -- they drain to the companion via /outbox/due.
    sched = om.scheduled_at
    if sched is not None and sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)  # SQLite stores naive
    if channel == "whatsapp" and sched <= _utcnow():
        _drain_whatsapp(db, user, om, contact)

    return {"id": om.id, "status": om.status, "channel": om.channel,
            "scheduled_at": om.scheduled_at.isoformat()}


def _drain_whatsapp(db, user, om, contact) -> None:
    """Dispatch one queued WhatsApp OutgoingMessage through Unipile (cloud
    send) and flip its status in place. Best-effort: any failure marks the row
    failed rather than raising, so a flaky provider can't 500 the send route."""
    from ..providers.unipile import UnipileProvider

    acct = getattr(user, "unipile_whatsapp_account_id", None) or ""
    if not acct or getattr(user, "whatsapp_status", "") != "active":
        om.status = "failed"
        om.error = "owner has no connected whatsapp account"
        db.commit()
        return
    to_phone = (om.to_handle or (contact.phone if contact else "") or "").strip()
    if not to_phone:
        om.status = "failed"
        om.error = "no recipient phone for whatsapp send"
        db.commit()
        return

    provider = UnipileProvider.from_env()
    res = provider.send_whatsapp(
        whatsapp_account_id=acct, to_phone=to_phone, body=om.body or "")
    if res.state in ("message_sent", "dry_run_queued"):
        om.status = "sent"
        om.sent_at = _utcnow()
    elif res.state == "unconfirmed":
        # Dispatched but response lost -- leave queued so a human decides,
        # rather than marking sent (may not have landed) or retrying (double-send).
        om.error = (res.error or "unconfirmed")[:300]
    else:
        om.status = "failed"
        om.error = (res.error or "whatsapp send failed")[:300]
    db.commit()


@router.get("/outbox/due")
def outbox_due(db: Session = Depends(get_db),
               user: models.User = Depends(current_user), limit: int = 50):
    """Device-channel sends that are due now -- what the user's companion executes."""
    rows = (db.query(models.OutgoingMessage)
            .filter(models.OutgoingMessage.user_id == user.id,
                    models.OutgoingMessage.status == "queued",
                    models.OutgoingMessage.channel.in_(tuple(_DEVICE_CHANNELS)),
                    models.OutgoingMessage.scheduled_at <= _utcnow())
            .order_by(models.OutgoingMessage.scheduled_at.asc())
            .limit(max(1, min(limit, 200))).all())
    return {"due": [{"id": r.id, "channel": r.channel, "to_handle": r.to_handle,
                     "body": r.body, "scheduled_at": r.scheduled_at.isoformat()}
                    for r in rows]}


@router.post("/outbox/{message_id}/sent")
def outbox_sent(message_id: int, body: SentIn, db: Session = Depends(get_db),
                user: models.User = Depends(current_user)):
    """Companion reports a device send result; flips status sent|failed."""
    om = db.get(models.OutgoingMessage, message_id)
    if om is None or om.user_id != user.id:
        raise HTTPException(404, "message not found")
    if body.ok:
        om.status = "sent"
        om.sent_at = _utcnow()
    else:
        om.status = "failed"
        om.error = (body.error or "")[:300]
    db.commit()
    return {"id": om.id, "status": om.status}
