"""Tests for the Outlook (Microsoft Graph) connector + the shared calendar ingest.
No live Graph: the client / token are mocked. Mirrors the Gmail connector tests."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import outlook_client, outlook_sync, oauth, sync_common
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


def _user(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    u.email_account_address = "me@x.com"
    db.add(u); db.commit()
    return u


# ── Graph client normalization ────────────────────────────────────────────────
def test_outlook_fetch_page_normalizes_graph_messages(monkeypatch):
    monkeypatch.setattr(outlook_client, "_get", lambda token, url, params=None: {
        "value": [{
            "id": "m1",
            "from": {"emailAddress": {"address": "Sarah@X.com", "name": "Sarah"}},
            "toRecipients": [{"emailAddress": {"address": "me@x.com", "name": "Me"}}],
            "sentDateTime": "2026-06-24T10:00:00Z"}],
        "@odata.nextLink": "https://graph/next"})
    page = outlook_client.outlook_fetch_page("tok", own_email="me@x.com")
    it = page["items"][0]
    assert it["from_attendee"] == {"identifier": "sarah@x.com", "display_name": "Sarah"}
    assert it["to_attendees"][0]["identifier"] == "me@x.com"
    assert it["role"] == "" and page["cursor"] == "https://graph/next"


def test_outlook_fetch_page_marks_own_as_sent(monkeypatch):
    monkeypatch.setattr(outlook_client, "_get", lambda *a, **k: {"value": [{
        "id": "m2", "from": {"emailAddress": {"address": "me@x.com"}},
        "toRecipients": [{"emailAddress": {"address": "sarah@x.com"}}],
        "sentDateTime": "2026-06-24T10:00:00Z"}]})
    page = outlook_client.outlook_fetch_page("tok", own_email="me@x.com")
    assert page["items"][0]["role"] == "sent"      # from == own


def test_outlook_calendar_flattens(monkeypatch):
    monkeypatch.setattr(outlook_client, "_get", lambda *a, **k: {"value": [{
        "id": "ev1", "subject": "Sync with Sarah",
        "start": {"dateTime": "2026-07-01T15:00:00.0000000"},
        "attendees": [{"emailAddress": {"address": "Sarah@X.com"}}]}]})
    evs = outlook_client.fetch_calendar_events("tok", time_min_iso="a", time_max_iso="b")
    assert evs[0]["id"] == "ev1" and "sarah@x.com" in evs[0]["attendees"]


# ── sync ──────────────────────────────────────────────────────────────────────
def test_sync_outlook_email_reuses_spine_pipeline(db, monkeypatch):
    u = _user(db)
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda db, a, **k: "tok")
    # Two-way exchange : the user's reply is what qualifies Sarah as a
    # contact under the two-way filter (direction derives from from == own).
    monkeypatch.setattr(outlook_client, "outlook_fetch_page", lambda *a, **k: {"items": [{
        "from_attendee": {"identifier": "sarah@x.com", "display_name": "Sarah"},
        "to_attendees": [{"identifier": "me@x.com", "display_name": ""}],
        "date": "2026-06-24T10:00:00Z", "role": "", "provider_id": "m1"}, {
        "from_attendee": {"identifier": "me@x.com", "display_name": ""},
        "to_attendees": [{"identifier": "sarah@x.com", "display_name": "Sarah"}],
        "date": "2026-06-24T11:00:00Z", "role": "", "provider_id": "m2"}],
        "cursor": None})
    stats = outlook_sync.sync_outlook_email(db, u, SimpleNamespace(account_email="me@x.com"))
    c = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert c.email == "sarah@x.com" and stats.get("people") == 1


def test_sync_outlook_calendar_writes_meeting_facts(db, monkeypatch):
    u = _user(db)
    known = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                           email="sarah@x.com", name="Sarah")
    db.add(known); db.commit()
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda db, a, **k: "tok")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    monkeypatch.setattr(outlook_client, "fetch_calendar_events", lambda *a, **k: [
        {"id": "ev1", "summary": "Sync with Sarah", "start": soon,
         "attendees": ["sarah@x.com", "stranger@y.com"]}])
    res = outlook_sync.sync_outlook_calendar(db, u, SimpleNamespace(account_email="me@x.com"))
    assert res["meeting_facts"] == 1
    assert cm.get_facts(db, known.id, key="upcoming_meeting")[0].source == "outlook"


# ── shared calendar ingest (covers all providers once) ────────────────────────
def test_sync_common_ingest_known_only_upcoming_idempotent(db):
    u = _user(db)
    c = models.Contact(user_id=u.id, primary_identity_key="li:s", email="s@x.com")
    db.add(c); db.commit()
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    events = [
        {"id": "e1", "summary": "Upcoming", "start": soon, "attendees": ["s@x.com", "x@y.com"]},
        {"id": "e2", "summary": "Past", "start": past, "attendees": ["s@x.com"]},
    ]
    r = sync_common.ingest_meeting_events(db, u.id, events, source="gcal")
    assert r["meeting_facts"] == 1                  # known + upcoming only
    sync_common.ingest_meeting_events(db, u.id, events, source="gcal")
    assert len(cm.get_facts(db, c.id, key="upcoming_meeting")) == 1   # idempotent
