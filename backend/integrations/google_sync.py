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
    """Pull recent Gmail into the Contact spine via the shared email_sync pipeline.

    NOT wired into sync_google_account by default: Gmail context comes via UNIPILE so we
    avoid the restricted gmail.readonly scope (CASA). This stays as a fallback if you ever
    add gmail.readonly to the Google scopes + complete CASA."""
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


def sync_google_contacts(db, user, account, *, max_pages: int = 10) -> dict:
    """Import the user's Google Contacts (phone address book) into the spine. Each
    contact is find-or-created by identity key (em: > ph:), so a phone-only contact is
    keyed by ph: and dedupes against existing people. Enrich, never clobber."""
    from .. import models
    from ..triage.enrichment_cache import identity_keys
    token = oauth.get_valid_access_token(db, account)
    if not token:
        return {"error": "no valid token"}
    stats = {"scanned": 0, "contacts_created": 0, "contacts_updated": 0}
    cursor = None
    for _ in range(max_pages):
        try:
            page = google_client.fetch_contacts(token, cursor=cursor)
        except Exception as exc:  # noqa: BLE001 : a flaky page must not 500
            stats["error"] = f"{type(exc).__name__}: {exc}"
            break
        for ct in page.get("items", []):
            stats["scanned"] += 1
            keys = identity_keys(email=ct.get("email", ""), phone=ct.get("phone", ""))
            if not keys:
                continue
            primary = keys[0]
            contact = (db.query(models.Contact)
                       .filter_by(user_id=user.id, primary_identity_key=primary).first())
            if contact is None:
                db.add(models.Contact(
                    user_id=user.id, primary_identity_key=primary,
                    name=ct.get("name") or None, email=ct.get("email") or None,
                    phone=ct.get("phone") or None))
                stats["contacts_created"] += 1
            else:
                if not contact.email and ct.get("email"):
                    contact.email = ct["email"]
                if not contact.name and ct.get("name"):
                    contact.name = ct["name"]
                if not contact.phone and ct.get("phone"):
                    contact.phone = ct["phone"]
                stats["contacts_updated"] += 1
        cursor = page.get("cursor")
        if not cursor:
            break
    db.commit()
    return stats


def sync_google_account(db, user, account) -> dict:
    """Read sync for one connected Google account: Calendar + Contacts. (Gmail is NOT
    here -- it comes via Unipile to avoid the restricted gmail.readonly scope / CASA.)"""
    return {"calendar": sync_google_calendar(db, user, account),
            "contacts": sync_google_contacts(db, user, account)}
