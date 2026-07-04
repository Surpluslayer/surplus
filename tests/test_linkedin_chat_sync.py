"""
Tests for sync_linkedin_chats : page the user's LinkedIn DM chats/messages from
Unipile and ingest each message into the relationship timeline keyed by the
peer's li: identity (public slug), channel='linkedin', idempotent by Unipile
message id, incremental via users.linkedin_chat_synced_at.

All Unipile calls are injected (list_chats / chat_attendees / chat_messages /
resolve_profile), so no network. Mirrors test_whatsapp_sync.py's
injected-fetcher pattern.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.linkedin_chat_sync import sync_linkedin_chats


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
                    unipile_account_id="li_acct_1",
                    linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _fixtures():
    chats = {"items": [{"id": "chat_1", "timestamp": "2026-06-20T12:00:00Z"}]}
    attendees = {"items": [
        {"is_self": True, "provider_id": "SELF_ID"},
        {"is_self": False, "provider_id": "MEMBER_1", "name": "Sarah Chen",
         "profile_url": "https://www.linkedin.com/in/ACoAAmember1"},
    ]}
    profile = {"public_identifier": "sarah-chen", "first_name": "Sarah",
               "last_name": "Chen", "headline": "Founder @ Kyndred"}
    messages = {"items": [
        {"id": "limsg.2", "text": "great, 3pm works",
         "is_sender": True, "timestamp": "2026-06-20T12:00:00Z"},
        {"id": "limsg.1", "text": "happy to chat thursday!",
         "is_sender": False, "timestamp": "2026-06-01T10:00:00Z"},
    ]}
    return chats, attendees, profile, messages


def _kw(chats, attendees, profile, messages):
    return dict(
        list_chats=lambda cursor: chats if cursor is None else {"items": []},
        chat_attendees=lambda cid: attendees,
        chat_messages=lambda cid, cursor: messages if cursor is None else {"items": []},
        resolve_profile=lambda pid: profile,
    )


def test_sync_ingests_messages_keyed_by_slug_channel_linkedin(db):
    u = _user(db)
    stats = sync_linkedin_chats(db, u, **_kw(*_fixtures()))
    assert stats["error"] is None
    assert stats["appended"] == 2
    assert stats["contacts_created"] == 1

    # Contact keyed by the LinkedIn slug (the li: identity scheme).
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert ct.primary_identity_key.startswith("li:")
    assert ct.linkedin_url == "https://www.linkedin.com/in/sarah-chen"
    assert ct.linkedin_public_id == "sarah-chen"
    assert ct.name == "Sarah Chen"
    assert ct.headline == "Founder @ Kyndred"

    # Two message interactions, channel carried by source_type='linkedin',
    # directions mapped (their msg -> inbound, ours -> outbound).
    ris = (db.query(models.RelationshipInteraction)
           .filter_by(contact_id=ct.id, interaction_type="message").all())
    assert len(ris) == 2
    assert {r.source_type for r in ris} == {"linkedin"}
    assert {r.direction for r in ris} == {"inbound", "outbound"}


def test_sync_is_idempotent_by_message_id(db):
    u = _user(db)
    kw = _kw(*_fixtures())
    sync_linkedin_chats(db, u, **kw)
    # incremental=False so the second pass re-fetches everything: the dedup
    # (skip-by-message-id) must still hold on its own.
    s2 = sync_linkedin_chats(db, u, incremental=False, **kw)
    assert s2["appended"] == 0
    assert s2["skipped"] == 2
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    assert (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=ct.id, interaction_type="message").count()) == 2


def test_sync_sets_watermark_and_incremental_skips_stale_chats(db):
    u = _user(db)
    kw = _kw(*_fixtures())
    before = datetime.now(timezone.utc)
    s1 = sync_linkedin_chats(db, u, **kw)
    assert s1["appended"] == 2
    wm = u.linkedin_chat_synced_at
    assert wm is not None
    wm_aware = wm if wm.tzinfo else wm.replace(tzinfo=timezone.utc)
    assert wm_aware >= before

    # Second incremental run: the chat's last activity (2026-06-20) predates
    # the watermark, so the chat isn't even fetched.
    calls = {"msgs": 0}
    def counting_messages(cid, cursor):
        calls["msgs"] += 1
        return {"items": []}
    kw2 = dict(kw, chat_messages=counting_messages)
    s2 = sync_linkedin_chats(db, u, **kw2)
    assert s2["error"] is None
    assert s2["appended"] == 0
    assert calls["msgs"] == 0


def test_incremental_skips_messages_older_than_watermark(db):
    u = _user(db)
    # Watermark between the two fixture messages: only the newer one lands.
    u.linkedin_chat_synced_at = datetime(2026, 6, 10, tzinfo=timezone.utc)
    db.commit()
    stats = sync_linkedin_chats(db, u, **_kw(*_fixtures()))
    assert stats["error"] is None
    assert stats["appended"] == 1
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    ri = (db.query(models.RelationshipInteraction)
          .filter_by(contact_id=ct.id, interaction_type="message").one())
    assert ri.summary == "great, 3pm works"


def test_sync_skips_unipile_media_placeholders(db):
    u = _user(db)
    chats, attendees, profile, messages = _fixtures()
    messages = {"items": messages["items"] + [
        {"id": "limsg.3",
         "text": "Unipile cannot display this type of message yet",
         "is_sender": False, "timestamp": "2026-06-21T09:00:00Z"},
    ]}
    stats = sync_linkedin_chats(db, u, **_kw(chats, attendees, profile, messages))
    assert stats["appended"] == 2  # placeholder never stored
    ct = db.query(models.Contact).filter_by(user_id=u.id).one()
    texts = [r.summary for r in db.query(models.RelationshipInteraction)
             .filter_by(contact_id=ct.id, interaction_type="message").all()]
    assert not any("cannot display" in t for t in texts)


def test_sync_skips_group_chats(db):
    u = _user(db)
    chats, _, profile, messages = _fixtures()
    attendees = {"items": [
        {"is_self": True, "provider_id": "SELF_ID"},
        {"is_self": False, "provider_id": "M1", "name": "A"},
        {"is_self": False, "provider_id": "M2", "name": "B"},
    ]}
    stats = sync_linkedin_chats(db, u, **_kw(chats, attendees, profile, messages))
    assert stats["appended"] == 0
    assert db.query(models.Contact).filter_by(user_id=u.id).count() == 0


def test_sync_skips_linkedin_system_account(db):
    u = _user(db)
    chats, _, _, messages = _fixtures()
    attendees = {"items": [
        {"is_self": True, "provider_id": "SELF_ID"},
        {"is_self": False, "provider_id": "LI_SYS", "name": "LinkedIn"},
    ]}
    profile = {"public_identifier": "1337"}
    stats = sync_linkedin_chats(db, u, **_kw(chats, attendees, profile, messages))
    assert stats["appended"] == 0
    assert db.query(models.Contact).filter_by(user_id=u.id).count() == 0


def test_ingest_opens_a_short_lived_session_per_chat(db):
    """Session-hygiene contract: the ingest phase must not hold one session
    for the whole run. Each chat's writes go through a FRESH session from the
    factory (open, commit, close), so a multi-chat run opens one session per
    chat and closes every one of them."""
    from sqlalchemy.orm import sessionmaker

    u = _user(db)
    chats = {"items": [
        {"id": "chat_1", "timestamp": "2026-06-20T12:00:00Z"},
        {"id": "chat_2", "timestamp": "2026-06-19T12:00:00Z"},
    ]}
    attendees = {
        "chat_1": {"items": [
            {"is_self": True, "provider_id": "SELF_ID"},
            {"is_self": False, "provider_id": "M1", "name": "Sarah Chen"},
        ]},
        "chat_2": {"items": [
            {"is_self": True, "provider_id": "SELF_ID"},
            {"is_self": False, "provider_id": "M2", "name": "Ben Ito"},
        ]},
    }
    profiles = {"M1": {"public_identifier": "sarah-chen"},
                "M2": {"public_identifier": "ben-ito"}}
    messages = {
        "chat_1": {"items": [{"id": "limsg.1", "text": "hey from sarah",
                              "is_sender": False,
                              "timestamp": "2026-06-20T12:00:00Z"}]},
        "chat_2": {"items": [{"id": "limsg.2", "text": "hey from ben",
                              "is_sender": False,
                              "timestamp": "2026-06-19T12:00:00Z"}]},
    }

    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    counts = {"opened": 0, "closed": 0}

    def counting_factory():
        counts["opened"] += 1
        s = factory()
        orig_close = s.close

        def close():
            counts["closed"] += 1
            orig_close()
        s.close = close
        return s

    stats = sync_linkedin_chats(
        db, u,
        list_chats=lambda cursor: chats if cursor is None else {"items": []},
        chat_attendees=lambda cid: attendees[cid],
        chat_messages=lambda cid, cursor: (messages[cid] if cursor is None
                                           else {"items": []}),
        resolve_profile=lambda pid: profiles[pid],
        session_factory=counting_factory,
    )
    assert stats["error"] is None
    assert stats["chats"] == 2
    assert stats["appended"] == 2
    # One fresh session PER chat (> 1 proves no single long-held session),
    # and every one of them was closed again.
    assert counts["opened"] == 2
    assert counts["closed"] == 2
    # The per-batch commits are real: the outer session sees both contacts.
    assert db.query(models.Contact).filter_by(user_id=u.id).count() == 2


def test_sync_no_account_or_inactive_returns_error(db):
    u = models.User(name="Host", email="h@x.com")  # no linkedin account
    db.add(u); db.commit(); db.refresh(u)
    stats = sync_linkedin_chats(db, u)
    assert stats["error"] == "no connected linkedin account"

    u2 = models.User(name="H2", email="h2@x.com",
                     unipile_account_id="li_acct_2",
                     linkedin_status="disconnected")
    db.add(u2); db.commit(); db.refresh(u2)
    stats2 = sync_linkedin_chats(db, u2)
    assert stats2["error"] == "linkedin account not active"
