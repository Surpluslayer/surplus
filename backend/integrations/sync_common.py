"""integrations/sync_common.py : shared spine-write helpers across calendar/email
providers (Google, Microsoft, ...). One implementation of contact lookup + the
meeting-fact write, so every provider behaves identically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import models
from ..agents.relationship.enrichment_cache import identity_keys


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO parse -> aware UTC. Handles 'Z', offsets, and date-only."""
    if not s:
        return None
    raw = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.fromisoformat(raw[:10] + "T00:00:00")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def contact_by_email(db, user_id: int, addr: str):
    """Resolve an email address to one of the user's Contacts (strong key first,
    then a direct email match). None if unknown."""
    addr = (addr or "").strip().lower()
    if not addr:
        return None
    keys = identity_keys(email=addr)
    c = None
    if keys:
        c = (db.query(models.Contact)
             .filter_by(user_id=user_id, primary_identity_key=keys[0]).first())
    if c is None:
        c = (db.query(models.Contact)
             .filter(models.Contact.user_id == user_id,
                     models.Contact.email == addr).first())
    return c


def ingest_meeting_events(db, user_id: int, events: list, *, source: str,
                          commit: bool = True) -> dict:
    """Write a dated `upcoming_meeting` fact per KNOWN attendee of upcoming events.
    Only attendees already in the spine get a fact (a calendar invite doesn't create
    contacts). Idempotent per event via dedup_key=event id. Returns counts.

    `events` items: {id, summary, start (ISO), attendees: [email, ...]}."""
    from ..agents.relationship.spine.memory import upsert_fact
    now = datetime.now(timezone.utc)
    written = 0
    for e in events:
        start = parse_iso(e.get("start"))
        if start is None or start < now:        # only upcoming
            continue
        for addr in (e.get("attendees") or []):
            c = contact_by_email(db, user_id, addr)
            if c is None:
                continue
            upsert_fact(db, user_id, c.id, "upcoming_meeting",
                        e.get("summary") or "meeting", source=source,
                        confidence="high", due_date=start,
                        dedup_key=(e.get("id") or "")[:60], commit=False)
            written += 1
    if commit:
        db.commit()
    return {"events": len(events), "meeting_facts": written}
