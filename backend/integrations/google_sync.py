"""integrations/google_sync.py : pull a connected Google account into the spine.

  EMAIL    -> reuses agents.relationship.email_sync.sync_email_contacts by feeding it
              Gmail data in the same mail-item shape (contacts + an email-thread
              rollup interaction land in the timeline; the message-ingestion sweep
              later mines facts from it).
  CALENDAR -> writes a dated `upcoming_meeting` ContactFact per known attendee, which
              the Flow-1 trigger engine fires on ("meeting with X tomorrow").

Best-effort; a fresh access token is fetched via oauth.get_valid_access_token.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import models
from ..triage.enrichment_cache import identity_keys
from . import google_client, oauth


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    raw = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:                              # date-only ("2026-06-28")
            dt = datetime.fromisoformat(raw + "T00:00:00")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _contact_by_email(db, user_id: int, addr: str):
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


def sync_google_email(db, user, account, *, newer_than_days: int = 30) -> dict:
    """Pull recent Gmail into the Contact spine via the shared email_sync pipeline."""
    from ..agents.relationship import email_sync
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    own = (account.account_email
           or getattr(user, "email_account_address", "") or "")

    def fetch_page(cursor):
        return google_client.gmail_fetch_page(
            token, own_email=own, cursor=cursor, newer_than_days=newer_than_days)

    return email_sync.sync_email_contacts(
        db, user, dsn="", api_key="", fetch_page=fetch_page)


def sync_google_calendar(db, user, account, *, days_back: int = 1,
                         days_ahead: int = 21) -> dict:
    """Write a dated `upcoming_meeting` fact per known attendee of upcoming events.
    Only attendees already in the contact spine get a fact (no new contacts from a
    calendar invite). Idempotent per event via dedup_key=event id."""
    from ..agents.relationship.spine.memory import upsert_fact
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    now = datetime.now(timezone.utc)
    events = google_client.fetch_calendar_events(
        token,
        time_min_iso=(now - timedelta(days=days_back)).isoformat(),
        time_max_iso=(now + timedelta(days=days_ahead)).isoformat())
    written = 0
    for e in events:
        start = _parse_iso(e.get("start"))
        if start is None or start < now:           # only upcoming
            continue
        for addr in (e.get("attendees") or []):
            c = _contact_by_email(db, user.id, addr)
            if c is None:
                continue
            upsert_fact(db, user.id, c.id, "upcoming_meeting",
                        e.get("summary") or "meeting", source="gcal",
                        confidence="high", due_date=start,
                        dedup_key=(e.get("id") or "")[:60], commit=False)
            written += 1
    db.commit()
    return {"events": len(events), "meeting_facts": written}


def sync_google_account(db, user, account) -> dict:
    """Full read sync for one connected Google account: Gmail + Calendar."""
    return {"email": sync_google_email(db, user, account),
            "calendar": sync_google_calendar(db, user, account)}
