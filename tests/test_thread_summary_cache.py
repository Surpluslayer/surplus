"""Integration test: thread summary cached on ContactFact."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import contact_memory as cm
from backend.agents.relationship import relationships as rel
from backend.agents.relationship.thread_summary import (
    _fingerprint,
    summarize_older_messages,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_summary_cached_on_contact_fact(db):
    user = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(user)
    db.flush()

    ev = models.Event(user_id=user.id, kind="in_person", label="Test", city="SF")
    db.add(ev)
    db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="cached-sum", name="Alex Chen",
        linkedin_url="https://linkedin.com/in/cached-sum", status="pending")
    db.add(p)
    db.flush()
    c = rel.link_contact(db, p, user.id)

    older = [
        {"who": "host", "when": "1", "text": "Great meeting at SaaStr."},
        {"who": "them", "when": "2", "text": "Would love to see the deck."},
        {"who": "host", "when": "3", "text": "I'll send the deck this week."},
    ]
    fp = _fingerprint(older, 5)

    cm.upsert_fact(
        db, user.id, c.id, "thread_summary",
        "Met at SaaStr; Alex wants the deck; host promised to send it.",
        dedup_key=fp, source="thread_compress", confidence="high")

    out = summarize_older_messages(
        older, recent_limit=5, db=db, user_id=user.id,
        contact_id=c.id, contact_name="Alex Chen")
    assert out == (
        "Earlier conversation: Met at SaaStr; Alex wants the deck; "
        "host promised to send it.")
