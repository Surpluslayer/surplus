"""Tests for contact dedup/merge (agents/relationship/contact_dedup.py). In-memory
SQLite; exercises group detection, FK reassignment, the fact unique-constraint
(newer wins), scalar backfill, and dry-run."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import contact_memory as cm, contact_dedup


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


def _user(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    return u


def _interaction(db, u, contact_id, title):
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=contact_id, source_type="manual_note",
        interaction_type="note", title=title, occurred_at=datetime.now(timezone.utc)))
    db.commit()


def test_find_groups_links_shared_email_key(db):
    u = _user(db)
    a = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                       linkedin_url="https://www.linkedin.com/in/sarah",
                       email="sarah@x.com", name="Sarah")
    b = models.Contact(user_id=u.id, primary_identity_key="em:xx",
                       email="sarah@x.com", name="Sarah R")    # shares em: key
    c = models.Contact(user_id=u.id, primary_identity_key="li:dave", name="Dave")
    db.add_all([a, b, c]); db.commit()
    groups = contact_dedup.find_duplicate_groups(db, u.id)
    assert len(groups) == 1
    assert {x.id for x in groups[0]} == {a.id, b.id}          # Dave is alone


def test_dry_run_reports_without_merging(db):
    u = _user(db)
    a = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                       email="sarah@x.com")
    b = models.Contact(user_id=u.id, primary_identity_key="em:xx",
                       email="sarah@x.com")
    db.add_all([a, b]); db.commit()
    res = contact_dedup.dedup_user(db, u.id)                  # dry_run default
    assert res["dry_run"] is True and res["would_merge"] == 1
    assert db.query(models.Contact).count() == 2             # nothing merged


def test_merge_reassigns_and_resolves_fact_conflict(db):
    u = _user(db)
    # A: richer (a prospect + interaction + fact) -> canonical. company empty.
    a = models.Contact(user_id=u.id, primary_identity_key="li:sarah",
                       linkedin_url="https://www.linkedin.com/in/sarah",
                       email="sarah@x.com", name="Sarah", company=None)
    b = models.Contact(user_id=u.id, primary_identity_key="em:xx",
                       email="sarah@x.com", company="TechCorp", vip=True)
    db.add_all([a, b]); db.commit()
    ev = models.Event(user_id=u.id, city="NYC"); db.add(ev); db.commit()
    p = models.Prospect(event_id=ev.id, identity="sarah", name="Sarah",
                        contact_id=a.id); db.add(p); db.commit()
    _interaction(db, u, a.id, "met at dinner")
    _interaction(db, u, b.id, "emailed re demo")
    # conflicting fact (based_in): A older NYC, B newer SF -> B should win
    fa = cm.upsert_fact(db, u.id, a.id, "based_in", "NYC")
    fa.observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc); db.commit()
    fb = cm.upsert_fact(db, u.id, b.id, "based_in", "SF")
    fb.observed_at = datetime(2026, 6, 1, tzinfo=timezone.utc); db.commit()
    cm.upsert_fact(db, u.id, b.id, "interest", "climbing")    # non-conflict -> moves

    res = contact_dedup.dedup_user(db, u.id, dry_run=False)
    assert res["merged_groups"] == 1
    assert db.query(models.Contact).count() == 1             # B deleted
    canon = db.query(models.Contact).one()
    assert canon.id == a.id                                   # richer one kept
    assert canon.company == "TechCorp"                        # backfilled from B
    assert canon.vip is True                                  # OR'd in
    # interaction + prospect reassigned to canonical
    assert db.query(models.RelationshipInteraction).filter_by(contact_id=a.id).count() == 2
    assert db.query(models.Prospect).filter_by(contact_id=a.id).count() == 1
    # fact conflict: newer SF won, single based_in row
    based = cm.get_facts(db, a.id, key="based_in")
    assert len(based) == 1 and based[0].value == "SF"
    assert {f.key for f in cm.get_facts(db, a.id)} == {"based_in", "interest"}
