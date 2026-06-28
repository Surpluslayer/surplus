"""integrations/calendly_sync.py : pull a connected Calendly account's upcoming
scheduled events into dated `upcoming_meeting` facts (via the shared calendar
ingest). Calendly is meetings-only -- no email channel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import calendly_client, oauth, sync_common


def sync_calendly_account(db, user, account, *, days_back: int = 1,
                          days_ahead: int = 21) -> dict:
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    user_uri = calendly_client.current_user_uri(token)
    if not user_uri:
        return {"error": "no calendly user"}
    now = datetime.now(timezone.utc)
    events = calendly_client.fetch_scheduled_meetings(
        token, user_uri=user_uri,
        time_min_iso=(now - timedelta(days=days_back)).isoformat(),
        time_max_iso=(now + timedelta(days=days_ahead)).isoformat())
    return {"calendar": sync_common.ingest_meeting_events(
        db, user.id, events, source="calendly")}
