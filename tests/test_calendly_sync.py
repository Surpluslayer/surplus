"""Tests for the Calendly connector (integrations/calendly_client + calendly_sync).
No live Calendly: the client / token are mocked."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import calendly_client, calendly_sync, oauth
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


def test_current_user_uri(monkeypatch):
    monkeypatch.setattr(calendly_client, "_get",
                        lambda *a, **k: {"resource": {"uri": "https://api.calendly.com/users/U1",
                                                      "email": "me@x.com"}})
    assert calendly_client.current_user_uri("tok").endswith("/U1")


def test_fetch_scheduled_meetings_enriches_invitees(monkeypatch):
    def fake_get(token, url, params=None):
        if url.endswith("/scheduled_events"):
            return {"collection": [{"uri": "https://api.calendly.com/scheduled_events/E1",
                                    "name": "Intro call", "start_time": "2026-07-01T15:00:00Z"}]}
        if url.endswith("/invitees"):
            return {"collection": [{"email": "Sarah@X.com"}, {"email": "x@y.com"}]}
        return {}
    monkeypatch.setattr(calendly_client, "_get", fake_get)
    mtgs = calendly_client.fetch_scheduled_meetings(
        "tok", user_uri="u", time_min_iso="a", time_max_iso="b")
    m = mtgs[0]
    assert m["id"] == "E1" and m["summary"] == "Intro call"
    assert "sarah@x.com" in m["attendees"]


def test_sync_calendly_writes_meeting_facts(db, monkeypatch):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    known = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                           email="sarah@x.com", name="Sarah")
    db.add(known); db.commit()
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda db, a, **k: "tok")
    monkeypatch.setattr(calendly_client, "current_user_uri", lambda tok: "u1")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    monkeypatch.setattr(calendly_client, "fetch_scheduled_meetings", lambda *a, **k: [
        {"id": "E1", "summary": "Intro call", "start": soon,
         "attendees": ["sarah@x.com", "stranger@y.com"]}])
    res = calendly_sync.sync_calendly_account(db, u, SimpleNamespace(account_email="me@x.com"))
    assert res["calendar"]["meeting_facts"] == 1
    f = cm.get_facts(db, known.id, key="upcoming_meeting")[0]
    assert f.value == "Intro call" and f.source == "calendly"
