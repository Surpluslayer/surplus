"""Tests for message-content fact extraction (pipeline/context/extract.py). LLM is
mocked. Covers key-filtering, confidence mapping, dedup-key slug, no-LLM/empty, and
idempotent upsert into the store."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import book
from backend.agents.relationship.spine import memory as cm
from backend.agents.relationship.pipeline.context import extract


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


THREAD = [
    {"who": "them", "text": "just moved to Brooklyn, loving the climbing gym"},
    {"who": "host", "text": "nice!"},
    {"who": "them", "text": "building a vector db startup, raising soon"},
]


def test_extract_filters_keys_confidence_and_slug(monkeypatch):
    monkeypatch.setattr(book, "_anthropic_available", lambda: True)
    monkeypatch.setattr(book, "_llm_json", lambda *a, **k: {"facts": [
        {"key": "based_in", "value": "Brooklyn", "confidence": "high"},
        {"key": "works_on", "value": "a vector DB startup", "confidence": "low"},
        {"key": "next_step", "value": "nope", "confidence": "high"},   # disallowed key
        {"key": "interest", "value": "", "confidence": "high"},        # empty -> skip
    ]})
    facts = extract.extract_facts(THREAD, contact_name="Sam")
    pairs = {(f["key"], f["value"]) for f in facts}
    assert ("based_in", "Brooklyn") in pairs and ("works_on", "a vector DB startup") in pairs
    assert all(f["key"] != "next_step" for f in facts)      # disallowed dropped
    assert all(f["value"] for f in facts)                   # empty dropped
    bd = next(f for f in facts if f["key"] == "based_in")
    assert bd["confidence"] == "high" and bd["dedup_key"] == "brooklyn"
    wo = next(f for f in facts if f["key"] == "works_on")
    assert wo["confidence"] == "low" and wo["dedup_key"] == "a-vector-db-startup"


def test_extract_empty_thread_and_no_llm(monkeypatch):
    assert extract.extract_facts([]) == []                  # nothing to read
    monkeypatch.setattr(book, "_anthropic_available", lambda: False)
    assert extract.extract_facts(THREAD) == []              # LLM unavailable -> safe []


def test_ingest_upserts_and_is_idempotent(db, monkeypatch):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:sam", name="Sam")
    db.add(c); db.commit()
    monkeypatch.setattr(extract.relationships, "contact_timeline", lambda db, c: [])
    monkeypatch.setattr(extract, "thread_from_timeline", lambda tl: THREAD)
    monkeypatch.setattr(extract, "extract_facts", lambda thread, contact_name="": [
        {"key": "based_in", "value": "Brooklyn", "confidence": "high", "dedup_key": "brooklyn"},
        {"key": "interest", "value": "climbing", "confidence": "low", "dedup_key": "climbing"},
    ])
    r1 = extract.ingest_contact_facts(db, c)
    assert r1["extracted"] == 2 and set(r1["keys"]) == {"based_in", "interest"}
    assert {f.key for f in cm.get_facts(db, c.id)} == {"based_in", "interest"}
    assert cm.get_facts(db, c.id, key="based_in")[0].source == "message"
    # re-run -> upsert in place, no duplication
    extract.ingest_contact_facts(db, c)
    assert len(cm.get_facts(db, c.id)) == 2


def test_ingest_sweep_bounds_and_aggregates(db, monkeypatch):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c1 = models.Contact(user_id=u.id, primary_identity_key="li:a", name="A")
    c2 = models.Contact(user_id=u.id, primary_identity_key="li:b", name="B")
    db.add_all([c1, c2]); db.commit()
    monkeypatch.setattr(extract.relationships, "list_contacts", lambda db, uid: [c1, c2])
    # c1 has message facts, c2 has none (no-op)
    def fake_ingest(db, c, commit=True):
        return {"contact_id": c.id, "extracted": 2 if c.id == c1.id else 0}
    monkeypatch.setattr(extract, "ingest_contact_facts", fake_ingest)
    res = extract.ingest_sweep(db, u.id)
    assert res == {"contacts": 2, "with_facts": 1, "extracted": 2}


def test_ingest_scheduled_sweep_off_by_default(monkeypatch):
    monkeypatch.delenv("MESSAGE_INGEST_ENABLED", raising=False)
    r = extract.run_claimed_ingest_sweep()
    assert r["ran"] is False and r["reason"] == "disabled"   # opt-in: no LLM cost unless enabled


def test_ingest_scheduled_sweep_lazy_imports_resolve(monkeypatch):
    """Guard the scheduler entry's lazy relative imports (the reorg-break class)."""
    monkeypatch.setenv("MESSAGE_INGEST_ENABLED", "1")
    import backend.agents.relationship.updates_scheduler as us
    monkeypatch.setattr(us, "_claim", lambda name, gap: False)   # not due -> returns before db
    r = extract.run_claimed_ingest_sweep()
    assert r["ran"] is False and "not due" in r["reason"]        # _claim import resolved
