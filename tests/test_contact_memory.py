"""Tests for the per-contact MEMORY store (agents/relationship/contact_memory.py
over the ContactFact table). In-memory SQLite, no network."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import contact_memory as cm


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


def _contact(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:sarah", name="Sarah")
    db.add(c); db.commit()
    return u, c


def test_contact_facts_table_exists():
    assert models.ContactFact.__tablename__ == "contact_facts"


def test_upsert_creates_then_updates_in_place(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "based_in", "NYC", source="linkedin")
    cm.upsert_fact(db, u.id, c.id, "based_in", "San Francisco", source="whatsapp")
    rows = cm.get_facts(db, c.id, key="based_in")
    assert len(rows) == 1                      # upserted in place, not duplicated
    assert rows[0].value == "San Francisco"    # latest value wins
    assert rows[0].source == "whatsapp"


def test_same_key_different_dedup_key_coexist(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "interest", "climbing", dedup_key="climbing")
    cm.upsert_fact(db, u.id, c.id, "interest", "jazz", dedup_key="jazz")
    interests = {r.value for r in cm.get_facts(db, c.id, key="interest")}
    assert interests == {"climbing", "jazz"}


def test_high_confidence_filter(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "birthday", "03-04", confidence="high")
    cm.upsert_fact(db, u.id, c.id, "works_on", "general", confidence="low")
    all_facts = cm.get_facts(db, c.id)
    high = cm.get_facts(db, c.id, high_confidence_only=True)
    assert len(all_facts) == 2
    assert [r.key for r in high] == ["birthday"]


def test_draft_grounding_surfaces_facts_and_tags_provenance(db):
    """High-confidence store facts become grounding clauses + provenance; keys
    already on the who-line (company/title) are skipped to avoid double-up."""
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "based_in", "San Francisco", source="linkedin")
    cm.upsert_fact(db, u.id, c.id, "interest", "climbing", dedup_key="climbing")
    cm.upsert_fact(db, u.id, c.id, "company", "TechCorp", source="linkedin")   # skipped
    cm.upsert_fact(db, u.id, c.id, "works_on", "low", confidence="low")        # gated out
    lines, prov = cm.draft_grounding(db, c.id)
    assert "based in San Francisco" in lines
    assert "into climbing" in lines
    assert all("TechCorp" not in ln for ln in lines)     # company not re-grounded
    assert all("low" != p["value"] for p in prov)        # low-confidence excluded
    # provenance tags source + mode for legibility
    assert {p["mode"] for p in prov} == {"graph"}
    assert {p["source"] for p in prov} == {"linkedin", "manual"}


def test_updates_engine_change_upserts_state_facts(db):
    """A LinkedIn job change emits the event AND upserts current-state facts, so
    the reader gets structured company/title from the store."""
    from backend.agents.relationship import updates_engine
    u, c = _contact(db)
    c.profile_baselined_at = None
    db.commit()
    # First scrape = baseline (silent) -> seeds the store from current state.
    updates_engine.apply_profile(db, c, {"company": "OldCo", "title": "Engineer"})
    db.commit()
    assert cm.get_facts(db, c.id, key="company")[0].value == "OldCo"
    # A later move upserts the new state in place.
    updates_engine.apply_profile(db, c, {"company": "TechCorp", "title": "VP Eng"})
    db.commit()
    company = cm.get_facts(db, c.id, key="company")
    assert len(company) == 1 and company[0].value == "TechCorp"   # upserted, not stacked
    assert company[0].source == "linkedin"


def test_due_date_hook_is_stored(db):
    """The time-trigger hook is just a stored column for now (no engine yet)."""
    from datetime import datetime, timezone
    u, c = _contact(db)
    due = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cm.upsert_fact(db, u.id, c.id, "upcoming_travel", "SF trip",
                   due_date=due, recurring=False)
    row = cm.get_facts(db, c.id, key="upcoming_travel")[0]
    assert row.due_date is not None
    assert row.recurring is False
