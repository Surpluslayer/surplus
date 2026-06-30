"""Tests for resolve_active_chain -- the "right chain" resolver.

Picks the channel to CONTINUE (where they're active > identity > fallback) and the
thread to reply into, so the agent doesn't start a new conversation.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.agents.relationship.pipeline.context.chain import resolve_active_chain
from backend.agents.relationship.spine import memory as cm


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _contact(db, **kw):
    c = models.Contact(user_id=1, primary_identity_key=kw.get("primary_identity_key", "k"), **{
        k: v for k, v in kw.items() if k != "primary_identity_key"})
    db.add(c); db.commit(); db.refresh(c)
    return c


def test_prefers_stored_channel_when_reachable(db):
    c = _contact(db, email="a@x.com", linkedin_url="https://linkedin.com/in/a")
    cm.upsert_fact(db, 1, c.id, "channel_preference", "email", source="behavior", commit=True)
    chain = resolve_active_chain(db, c)
    assert chain["channel"] == "email" and chain["reason"] == "active_channel"
    assert chain["to_handle"] == "a@x.com"


def test_attaches_email_thread_to_continue(db):
    c = _contact(db, email="a@x.com", email_thread_id="THREAD99")
    cm.upsert_fact(db, 1, c.id, "channel_preference", "email", source="behavior", commit=True)
    chain = resolve_active_chain(db, c)
    assert chain["channel"] == "email" and chain["thread_id"] == "THREAD99"


def test_falls_back_to_identity_when_pref_unreachable(db):
    # active pref says email, but contact has NO email -> fall to an available identity
    c = _contact(db, phone="+14155551234")
    cm.upsert_fact(db, 1, c.id, "channel_preference", "email", source="behavior", commit=True)
    chain = resolve_active_chain(db, c)
    assert chain["channel"] == "imessage" and chain["to_handle"] == "+14155551234"
    assert chain["reason"] == "identity"


def test_identity_priority_email_over_linkedin(db):
    c = _contact(db, email="a@x.com", linkedin_url="https://linkedin.com/in/a")
    chain = resolve_active_chain(db, c)         # no pref -> identity order email>linkedin
    assert chain["channel"] == "email"


def test_fallback_when_no_identity(db):
    c = _contact(db)                            # nothing to reach them on
    chain = resolve_active_chain(db, c, fallback="linkedin")
    assert chain["channel"] == "linkedin" and chain["reason"] == "fallback"
