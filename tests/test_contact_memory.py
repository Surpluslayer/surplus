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
    cm.upsert_fact(db, u.id, c.id, "about", "builds infra", confidence="low")  # -> optional
    asserted, optional, prov = cm.draft_grounding(db, c.id)
    assert "based in San Francisco" in asserted           # high-conf -> asserted
    assert "into climbing" in asserted
    assert all("TechCorp" not in ln for ln in asserted)   # company not re-grounded
    assert "what they work on: builds infra" in optional  # low-conf -> optional
    # provenance tags source + confidence + mode for legibility
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


def test_updates_engine_captures_about_as_low_confidence(db):
    """The LinkedIn About is captured on every scrape as a LOW-confidence fact
    (optional color), no migration needed -- it just lives in the fact store."""
    from backend.agents.relationship import updates_engine
    u, c = _contact(db)
    c.profile_baselined_at = None
    db.commit()
    updates_engine.apply_profile(db, c, {
        "company": "TechCorp", "title": "VP Eng",
        "about": "builds inference infrastructure"})
    db.commit()
    about = cm.get_facts(db, c.id, key="about")
    assert about and about[0].value == "builds inference infrastructure"
    assert about[0].confidence == "low" and about[0].source == "linkedin"
    # low-confidence -> flows to OPTIONAL grounding, never asserted
    asserted, optional, _ = cm.draft_grounding(db, c.id)
    assert any("inference infrastructure" in o for o in optional)
    assert all("inference infrastructure" not in a for a in asserted)


def test_channel_preference_picks_most_recent_inbound(db, monkeypatch):
    """The behavioral writer learns which channel a contact responds on from
    their inbound messages: the most recent one wins. Stored as a META fact."""
    from backend.agents.relationship import behavioral
    u, c = _contact(db)
    thread = [
        {"when": "2026-06-01", "who": "them", "channel": "linkedin", "text": "hi"},
        {"when": "2026-06-10", "who": "them", "channel": "whatsapp", "text": "yo"},
        {"when": "2026-06-20", "who": "host", "channel": "email", "text": "hey"},
    ]
    monkeypatch.setattr(behavioral, "contact_timeline", lambda db, c: [])
    monkeypatch.setattr(behavioral, "_thread_from_timeline", lambda tl: thread)
    ch = behavioral.derive_channel_preference(db, c, commit=True)
    assert ch == "whatsapp"       # most recent INBOUND (host's email doesn't count)
    assert cm.get_facts(db, c.id, key="channel_preference")[0].value == "whatsapp"


def test_channel_preference_is_meta_not_grounded(db):
    """META facts (how/where to reach them) are stored + readable but never
    surfaced into draft grounding -- you act on them, you don't mention them."""
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "channel_preference", "whatsapp", source="behavior")
    cm.upsert_fact(db, u.id, c.id, "based_in", "NYC", source="linkedin")
    asserted, optional, prov = cm.draft_grounding(db, c.id)
    assert "based in NYC" in asserted                          # attribute IS grounded
    assert all("whatsapp" not in ln for ln in asserted + optional)  # META is NOT grounded
    assert "channel_preference" not in {p["key"] for p in prov}


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


# ── Flow-1 trigger engine + fact lifecycle ────────────────────────────────────

def test_delete_fact_removes_it(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "based_in", "NYC")
    assert cm.delete_fact(db, c.id, "based_in") is True
    assert cm.get_facts(db, c.id, key="based_in") == []
    assert cm.delete_fact(db, c.id, "based_in") is False   # already gone


def test_due_facts_finds_only_what_is_due(db):
    from datetime import datetime, timezone, timedelta
    u, c = _contact(db)
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    cm.upsert_fact(db, u.id, c.id, "birthday", "today", due_date=now, recurring=True)
    cm.upsert_fact(db, u.id, c.id, "flight", "later",
                   due_date=now + timedelta(days=10), dedup_key="f1")
    cm.upsert_fact(db, u.id, c.id, "based_in", "NYC")     # not dated -> never due
    due = cm.due_facts(db, now=now, user_id=u.id)
    assert {f.key for f in due} == {"birthday"}
    # lookahead catches the upcoming flight
    assert {f.key for f in cm.due_facts(db, now=now, within_days=14)} == {"birthday", "flight"}


def test_scan_and_fire_recurring_advances_oneoff_deletes(db):
    from datetime import datetime, timezone
    from backend.agents.relationship import triggers
    u, c = _contact(db)
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    cm.upsert_fact(db, u.id, c.id, "birthday", "🎂", due_date=now, recurring=True)
    cm.upsert_fact(db, u.id, c.id, "flight", "SFO", due_date=now, dedup_key="f1")

    seen = []
    fired = triggers.scan_and_fire(db, user_id=u.id, now=now,
                                   on_due=lambda f, ct: seen.append((f.key, ct.id)))
    # both fired, callback got each with the real contact
    assert {k for k, _ in seen} == {"birthday", "flight"}
    assert {f["disposition"] for f in fired} == {"advanced", "deleted"}
    # one-off flight is gone; recurring birthday remains but advanced ~1yr ahead
    assert cm.get_facts(db, c.id, key="flight") == []
    bday = cm.get_facts(db, c.id, key="birthday")[0]
    assert bday.due_date.year == now.year + 1
    # and it does NOT re-fire on the same day
    assert cm.due_facts(db, now=now, user_id=u.id) == []
