"""agents/relationship/message_sink.py : the shared message-ingest sink.

Extracted from the retired routes/messages.py (the Mac-companion device
outbox) when that surface was deleted: the SINK half is live plumbing — the
LinkedIn chat sync and WhatsApp sync both land every ingested message
through append_message_for_contact / ingest_messages, which owns dedup (by
provider message id), contact find-or-create by handle, and the
RelationshipInteraction shape the drafting context reads.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

from ... import models
from .channels import DEVICE_CHANNELS as _DEVICE_CHANNELS
from .channels import MESSAGING_CHANNELS as _ALL_CHANNELS
from ...integrations.sync_common import parse_iso



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
    from .enrichment_cache import identity_keys
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


def append_message_for_contact(db, user, contact, m, stats) -> None:
    """Land ONE normalized message on a RESOLVED contact's timeline, idempotent
    per (contact, external_id). The single home of the dedup + insert rules:
    ingest_messages (phone/email-keyed sources) and the LinkedIn chat sync
    (linkedin-url-keyed peers) both funnel through here, so a source message id
    is skipped exactly the same way regardless of channel. Mutates `stats`
    ("appended"/"skipped") in place; the caller owns the commit."""
    # idempotency: skip if we already stored this source message id for this contact
    if m.external_id:
        marker = f'"ext": "{m.external_id}"'
        dup = (db.query(models.RelationshipInteraction)
               .filter(models.RelationshipInteraction.contact_id == contact.id,
                       models.RelationshipInteraction.meta_json.like(f"%{marker}%"))
               .first())
        if dup is not None:
            stats["skipped"] += 1
            return
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
        append_message_for_contact(db, user, contact, m, stats)
    db.commit()
    return stats

