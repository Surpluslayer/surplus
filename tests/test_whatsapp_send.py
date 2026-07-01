"""
Tests that WhatsApp OUTBOUND routes through Unipile server-side (CLOUD send),
NOT through the device companion's /outbox/due queue.

  * A due whatsapp send dispatches via UnipileProvider.send_whatsapp and the
    row flips to status='sent'.
  * whatsapp never appears in /outbox/due (that's device-only).

The Unipile send is mocked (monkeypatched onto UnipileProvider) -- no network.
Mirrors test_messages.py's TestClient + in-memory SQLite fixture.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend import models
from backend import auth as auth_mod
from backend.main import app
from backend.providers.base import ProviderResult


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        s = Session()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = _override
    # A user with an ACTIVE WhatsApp seat so the cloud send can dispatch.
    s = Session()
    u = models.User(name="Host", email="h@x.com",
                    unipile_whatsapp_account_id="wa_acct_1",
                    whatsapp_status="active")
    s.add(u); s.commit()
    tok = auth_mod.create_session(s, u).session_token; uid = u.id; s.close()
    c = TestClient(app); c.cookies.set("surplus_session", tok)
    yield c, Session, uid, monkeypatch
    app.dependency_overrides.clear()


def _capture_send(monkeypatch):
    """Patch UnipileProvider.send_whatsapp to record the call + return sent."""
    calls = []

    def fake_send(self, *, whatsapp_account_id, to_phone, body, prospect_id=0):
        calls.append({"account": whatsapp_account_id, "to": to_phone, "body": body})
        return ProviderResult(prospect_id=prospect_id, state="message_sent",
                              provider="unipile", provider_lead_id="chat_99",
                              dry_run=False, payload={})
    from backend.providers import unipile as up
    monkeypatch.setattr(up.UnipileProvider, "send_whatsapp", fake_send)
    return calls


def test_whatsapp_send_routes_cloud_and_marks_sent(client):
    c, Session, uid, monkeypatch = client
    calls = _capture_send(monkeypatch)

    r = c.post("/api/messages/send",
               json={"channel": "whatsapp", "to_handle": "+14155550123",
                     "body": "hey from whatsapp"})
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "whatsapp"
    # Cloud send happened server-side, row flipped to sent.
    assert body["status"] == "sent"
    assert len(calls) == 1
    assert calls[0]["to"] == "+14155550123"
    assert calls[0]["account"] == "wa_acct_1"

    s = Session()
    om = s.get(models.OutgoingMessage, body["id"])
    assert om.status == "sent" and om.sent_at is not None
    s.close()


def test_whatsapp_never_in_outbox_due(client):
    """The device queue (/outbox/due) must NOT serve whatsapp -- it's a cloud
    channel. Even a due whatsapp send (already dispatched server-side) stays
    out of /due."""
    c, Session, uid, monkeypatch = client
    _capture_send(monkeypatch)
    c.post("/api/messages/send",
           json={"channel": "whatsapp", "to_handle": "+1", "body": "cloud"})
    # plus a due device send for contrast
    c.post("/api/messages/send",
           json={"channel": "imessage", "to_handle": "+1", "body": "device"})
    due = c.get("/api/messages/outbox/due").json()["due"]
    assert {d["body"] for d in due} == {"device"}
    assert all(d["channel"] != "whatsapp" for d in due)


def test_scheduled_whatsapp_stays_queued(client):
    """A whatsapp send scheduled for later is NOT dispatched now -- it stays
    queued for the server to drain at scheduled_at (and still not in /due)."""
    c, Session, uid, monkeypatch = client
    calls = _capture_send(monkeypatch)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = c.post("/api/messages/send",
               json={"channel": "whatsapp", "to_handle": "+1", "body": "later",
                     "scheduled_at": future})
    assert r.json()["status"] == "queued"
    assert calls == []  # not dispatched yet
    assert c.get("/api/messages/outbox/due").json()["due"] == []
