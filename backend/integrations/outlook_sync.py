"""integrations/outlook_sync.py : pull a connected Microsoft account (Outlook mail +
calendar) into the spine. Mirrors google_sync -- email reuses email_sync, calendar
reuses sync_common.ingest_meeting_events -- so behavior is identical across providers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import oauth, outlook_client, sync_common


def sync_outlook_email(db, user, account, *, newer_than_days: int = 30) -> dict:
    from ..agents.relationship import email_sync
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    own = (account.account_email
           or getattr(user, "email_account_address", "") or "")

    def fetch_page(cursor):
        return outlook_client.outlook_fetch_page(
            token, own_email=own, cursor=cursor, newer_than_days=newer_than_days)

    return email_sync.sync_email_contacts(
        db, user, dsn="", api_key="", fetch_page=fetch_page)


def sync_outlook_calendar(db, user, account, *, days_back: int = 1,
                          days_ahead: int = 21) -> dict:
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    now = datetime.now(timezone.utc)
    events = outlook_client.fetch_calendar_events(
        token,
        time_min_iso=(now - timedelta(days=days_back)).isoformat(),
        time_max_iso=(now + timedelta(days=days_ahead)).isoformat())
    return sync_common.ingest_meeting_events(db, user.id, events, source="outlook")


def sync_outlook_account(db, user, account) -> dict:
    """Full read sync for one connected Microsoft account: Outlook mail + calendar."""
    return {"email": sync_outlook_email(db, user, account),
            "calendar": sync_outlook_calendar(db, user, account)}
