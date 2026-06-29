"""Tests for message capture (context in) + the send queue (out).

Ingest lands messages in the timeline keyed by phone/email (idempotent); the outbox
queues sends and the companion drains DEVICE channels (cloud channels aren't in /due).
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


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        s = Session()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = _override
    s = Session(); u = models.User(name="Host", email="h@x.com"); s.add(u); s.commit()
    tok = auth_mod.create_session(s, u).session_token; uid = u.id; s.close()
    c = TestClient(app); c.cookies.set("surplus_session", tok)
    yield c, Session, uid
    app.dependency_overrides.clear()


# ── ingest (context in) ───────────────────────────────────────────────────────
def test_ingest_creates_contact_by_phone_and_appends_timeline(client):
    c, Session, uid = client
    r = c.post("/api/messages/ingest", json={"messages": [
        {"handle": "+1 415 555 1234", "name": "Sarah", "direction": "in",
         "text": "happy to chat thursday!", "channel": "imessage", "external_id": "m1"}]})
    assert r.status_code == 200
    assert r.json() == {"contacts_created": 1, "appended": 1, "skipped": 0}
    s = Session()
    ct = s.query(models.Contact).filter_by(user_id=uid).one()
    assert ct.primary_identity_key.startswith("ph:") and ct.phone == "+1 415 555 1234"
    ri = s.query(models.RelationshipInteraction).filter_by(contact_id=ct.id).one()
    assert ri.interaction_type == "message" and "thursday" in ri.summary
    s.close()


def test_ingest_is_idempotent_by_external_id(client):
    c, Session, uid = client
    msg = {"messages": [{"handle": "a@x.com", "text": "hi", "channel": "email",
                         "external_id": "e1"}]}
    assert c.post("/api/messages/ingest", json=msg).json()["appended"] == 1
    r2 = c.post("/api/messages/ingest", json=msg)            # same id -> skipped
    assert r2.json() == {"contacts_created": 0, "appended": 0, "skipped": 1}
    s = Session()
    ct = s.query(models.Contact).filter_by(user_id=uid).one()
    assert s.query(models.RelationshipInteraction).filter_by(contact_id=ct.id).count() == 1
    s.close()


def test_ingest_skips_unkeyable_handle(client):
    c, _, _ = client
    r = c.post("/api/messages/ingest", json={"messages": [{"handle": "nodigits", "text": "x"}]})
    assert r.json()["skipped"] == 1 and r.json()["appended"] == 0


# ── send queue + outbox drain ─────────────────────────────────────────────────
def test_send_queues_device_message(client):
    c, Session, uid = client
    r = c.post("/api/messages/send",
               json={"channel": "imessage", "to_handle": "+14155551234", "body": "hey"})
    assert r.status_code == 200 and r.json()["status"] == "queued"


def test_device_send_needs_handle(client):
    c, _, _ = client
    assert c.post("/api/messages/send", json={"channel": "sms", "body": "x"}).status_code == 400


def test_outbox_due_returns_device_sends_only(client):
    c, Session, uid = client
    # one due device send, one cloud send, one future device send
    c.post("/api/messages/send", json={"channel": "imessage", "to_handle": "+1", "body": "due"})
    c.post("/api/messages/send", json={"channel": "whatsapp", "to_handle": "+1", "body": "cloud"})
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    c.post("/api/messages/send", json={"channel": "sms", "to_handle": "+1", "body": "later",
                                       "scheduled_at": future})
    due = c.get("/api/messages/outbox/due").json()["due"]
    bodies = {d["body"] for d in due}
    assert bodies == {"due"}                # only the due DEVICE send (no cloud, no future)


def test_outbox_mark_sent(client):
    c, Session, uid = client
    mid = c.post("/api/messages/send",
                 json={"channel": "imessage", "to_handle": "+1", "body": "x"}).json()["id"]
    r = c.post(f"/api/messages/outbox/{mid}/sent", json={"ok": True})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    # no longer due
    assert c.get("/api/messages/outbox/due").json()["due"] == []
    s = Session(); assert s.get(models.OutgoingMessage, mid).sent_at is not None; s.close()


def test_ingested_message_reaches_timeline_with_right_channel(client):
    """Regression: meta_json must not collide with _item(channel=...), and the device
    channel must survive into the timeline (so context + channel-preference see it)."""
    from backend.agents.relationship.spine import relationships as rel
    from backend.agents.relationship.pipeline.context.chain import resolve_active_chain
    c, Session, uid = client
    c.post("/api/messages/ingest", json={"messages": [
        {"handle": "+14155551234", "name": "Sarah", "direction": "in",
         "text": "coffee thursday?", "channel": "imessage", "external_id": "x1"}]})
    s = Session()
    ct = s.query(models.Contact).filter_by(user_id=uid).one()
    timeline = rel.contact_timeline(s, ct)           # must NOT raise
    msg = [it for it in timeline if it.get("interaction_type") == "message"]
    assert msg and msg[0]["channel"] == "imessage"   # channel survived, not "manual"
    # right-chain now sees iMessage as her ACTIVE channel (not just identity fallback)
    chain = resolve_active_chain(s, ct)
    assert chain["channel"] == "imessage" and chain["reason"] == "active_channel"
    s.close()
