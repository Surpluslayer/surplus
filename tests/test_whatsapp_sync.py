"""
Tests for sync_whatsapp_contacts : page the user's WhatsApp chats/messages from
Unipile and ingest each message into the relationship timeline keyed by the
counterpart's PHONE, channel='whatsapp', idempotent by Unipile message id.

All Unipile calls are injected (list_chats / chat_attendees / chat_messages),
so no network. Mirrors test_email_sync.py's injected-fetcher pattern.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.whatsapp_sync import (
    sync_whatsapp_contacts, _phone_from_attendee)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _user(db):
    u = models.User(name="Host", email="h@x.com",
                    unipile_whatsapp_account_id="wa_acct_1",
                    whatsapp_status="active")
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_phone_from_attendee_normalizes_jid():
    assert _phone_from_attendee("14155550123@s.whatsapp.net") == "+14155550123"
    assert _phone_from_attendee("+1 (415) 555-0123") == "+14155550123"
    assert _phone_from_attendee("") == ""
    assert _phone_from_attendee("group@g.us") == ""  # no digits -> skipped


def _fixtures():
    chats = {"items": [{"id": "chat_1"}]}
    attendees = {"items": [
        {"is_self": True, "provider_id": "self_jid"},
        {"is_self": False, "provider_id": "14155550123@s.whatsapp.net",
         "name": "Sarah"},
    ]}
    messages = {"items": [
        {"id": "wamid.1", "text": "happy to chat thursday!",
         "is_sender": False, "timestamp": "2026-06-01T10:00:00Z"},
        {"id": "wamid.2", "text": "great, 3pm works",
         "is_sender": True, "timestamp": "2026-06-01T10:05:00Z"},
    ]}
    return chats, attendees, messages


def test_sync_ingests_messages_keyed_by_phone_channel_whatsapp(db):
    u = _user(db)
    chats, attendees, messages = _fixtures()
    stats = sync_whatsapp_contacts(
        db, u,
        list_chats=lambda cursor: chats if cursor is None else {"items": []},
        chat_attendees=lambda cid: attendees,
        chat_messages=lambda cid, cursor: messages if cursor is None else {"items": []},
    )
    assert stats["error"] is None
    assert stats["appended"] == 2
    assert stats["contacts_created"] == 1

    # Contact keyed by PHONE.
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert ct.primary_identity_key.startswith("ph:")
    assert ct.phone == "+14155550123"
    assert ct.name == "Sarah"

    # Two message interactions, channel carried by source_type='whatsapp',
    # directions mapped (their msg -> inbound, ours -> outbound).
    ris = (db.query(models.RelationshipInteraction)
           .filter_by(contact_id=ct.id, interaction_type="message").all())
    assert len(ris) == 2
    assert {r.source_type for r in ris} == {"whatsapp"}
    assert {r.direction for r in ris} == {"inbound", "outbound"}


def test_sync_is_idempotent_by_message_id(db):
    u = _user(db)
    chats, attendees, messages = _fixtures()
    kw = dict(
        list_chats=lambda cursor: chats if cursor is None else {"items": []},
        chat_attendees=lambda cid: attendees,
        chat_messages=lambda cid, cursor: messages if cursor is None else {"items": []},
    )
    sync_whatsapp_contacts(db, u, **kw)
    s2 = sync_whatsapp_contacts(db, u, **kw)  # same message ids -> all skipped
    assert s2["appended"] == 0
    assert s2["skipped"] == 2
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=ct.id, interaction_type="message").count()) == 2


def test_sync_skips_group_chats(db):
    u = _user(db)
    chats = {"items": [{"id": "grp"}]}
    attendees = {"items": [
        {"is_self": True, "provider_id": "self"},
        {"is_self": False, "provider_id": "111@s.whatsapp.net", "name": "A"},
        {"is_self": False, "provider_id": "222@s.whatsapp.net", "name": "B"},
    ]}
    stats = sync_whatsapp_contacts(
        db, u,
        list_chats=lambda cursor: chats if cursor is None else {"items": []},
        chat_attendees=lambda cid: attendees,
        chat_messages=lambda cid, cursor: {"items": [{"id": "x", "text": "hi"}]},
    )
    assert stats["appended"] == 0
    assert db.query(models.Contact).filter_by(user_id=u.id).count() == 0


def test_sync_no_account_returns_error(db):
    u = models.User(name="Host", email="h@x.com")  # no whatsapp account
    db.add(u); db.commit(); db.refresh(u)
    stats = sync_whatsapp_contacts(db, u)
    assert stats["error"] == "no connected whatsapp account"
