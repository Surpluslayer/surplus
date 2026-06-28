"""Tests for the Google read connector (integrations/google_client + google_sync).
No live Google: httpx / the client / the token are mocked. Covers header parsing,
the normalized Gmail/Calendar shapes, the Gmail->spine reuse, and calendar->dated
meeting facts (known attendees only, upcoming only, idempotent)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import google_client, google_sync, oauth
from backend.agents.relationship.spine import memory as cm


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


# ── client normalization ──────────────────────────────────────────────────────
def test_addr_and_addrs_parse_headers():
    assert google_client._addr("Sarah Lee <Sarah@X.com>") == {
        "identifier": "sarah@x.com", "display_name": "Sarah Lee"}
    out = google_client._addrs("A <a@x.com>, b@y.com")
    assert {p["identifier"] for p in out} == {"a@x.com", "b@y.com"}


def test_gmail_fetch_page_normalizes_to_mail_item_shape(monkeypatch):
    def fake_get(token, url, params=None):
        if url.endswith("/messages"):
            return {"messages": [{"id": "m1"}], "nextPageToken": "NEXT"}
        return {"labelIds": ["INBOX"], "payload": {"headers": [
            {"name": "From", "value": "Sarah <sarah@x.com>"},
            {"name": "To", "value": "me@x.com"},
            {"name": "Date", "value": "Wed, 24 Jun 2026 10:00:00 +0000"}]}}
    monkeypatch.setattr(google_client, "_get", fake_get)
    page = google_client.gmail_fetch_page("tok", own_email="me@x.com")
    assert page["cursor"] == "NEXT"
    it = page["items"][0]
    assert it["from_attendee"] == {"identifier": "sarah@x.com", "display_name": "Sarah"}
    assert it["to_attendees"][0]["identifier"] == "me@x.com"
    assert it["role"] == ""                       # inbound (not from me, no SENT label)


def test_fetch_calendar_events_flattens(monkeypatch):
    monkeypatch.setattr(google_client, "_get", lambda *a, **k: {"items": [{
        "id": "ev1", "summary": "Coffee with Sarah",
        "start": {"dateTime": "2026-07-01T15:00:00Z"},
        "attendees": [{"email": "Sarah@X.com"}, {"email": "me@x.com"}]}]})
    evs = google_client.fetch_calendar_events("tok", time_min_iso="a", time_max_iso="b")
    assert evs[0]["id"] == "ev1" and evs[0]["summary"] == "Coffee with Sarah"
    assert "sarah@x.com" in evs[0]["attendees"]


# ── sync ──────────────────────────────────────────────────────────────────────
def _user(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    u.email_account_address = "me@x.com"
    db.add(u); db.commit()
    return u


def test_sync_email_reuses_spine_pipeline(db, monkeypatch):
    u = _user(db)
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda db, a, **k: "tok")
    monkeypatch.setattr(google_client, "gmail_fetch_page", lambda *a, **k: {"items": [{
        "from_attendee": {"identifier": "sarah@x.com", "display_name": "Sarah"},
        "to_attendees": [{"identifier": "me@x.com", "display_name": ""}],
        "date": "Wed, 24 Jun 2026 10:00:00 +0000", "role": "", "provider_id": "m1"}],
        "cursor": None})
    acct = SimpleNamespace(account_email="me@x.com")
    stats = google_sync.sync_google_email(db, u, acct)
    # Gmail message created a Contact in the spine (no Unipile account needed)
    c = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert c.email == "sarah@x.com"
    assert stats.get("people") == 1


def test_sync_calendar_writes_meeting_facts_for_known_attendees_only(db, monkeypatch):
    u = _user(db)
    known = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                           email="sarah@x.com", name="Sarah")
    db.add(known); db.commit()
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda db, a, **k: "tok")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [
        {"id": "ev1", "summary": "Coffee with Sarah", "start": soon,
         "attendees": ["sarah@x.com", "stranger@y.com"]},
        {"id": "ev2", "summary": "Past mtg", "start": past, "attendees": ["sarah@x.com"]},
    ])
    acct = SimpleNamespace(account_email="me@x.com")
    res = google_sync.sync_google_calendar(db, u, acct)
    assert res["meeting_facts"] == 1                # only Sarah, only the upcoming event
    facts = cm.get_facts(db, known.id, key="upcoming_meeting")
    assert len(facts) == 1 and facts[0].value == "Coffee with Sarah"
    assert facts[0].due_date is not None and facts[0].source == "gcal"
    # idempotent: same event re-synced -> upsert in place, no duplicate
    google_sync.sync_google_calendar(db, u, acct)
    assert len(cm.get_facts(db, known.id, key="upcoming_meeting")) == 1
