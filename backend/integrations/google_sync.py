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

from . import google_client, oauth, sync_common


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
    """Write dated `upcoming_meeting` facts for known attendees (via the shared
    calendar ingest)."""
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    now = datetime.now(timezone.utc)
    events = google_client.fetch_calendar_events(
        token,
        time_min_iso=(now - timedelta(days=days_back)).isoformat(),
        time_max_iso=(now + timedelta(days=days_ahead)).isoformat())
    return sync_common.ingest_meeting_events(db, user.id, events, source="gcal")


def sync_google_account(db, user, account) -> dict:
    """Full read sync for one connected Google account: Gmail + Calendar."""
    return {"email": sync_google_email(db, user, account),
            "calendar": sync_google_calendar(db, user, account)}
