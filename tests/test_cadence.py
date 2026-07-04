"""Tests for the cadence / who's-due engine (agents/relationship/cadence.py).
Pure deterministic logic; the relationships read surface is stubbed so we exercise
the cadence math + filter + ranking in isolation (not the whole timeline build)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from backend.agents.relationship.pipeline.proactive import cadence


def test_cadence_days_vip_active_loose():
    vip = SimpleNamespace(vip=True)
    plain = SimpleNamespace(vip=False)
    # VIP wins regardless of stage
    assert cadence.cadence_days(vip, {"relationship_stage": "captured"}) == cadence.CADENCE_VIP
    # real two-way relationship -> standard cadence
    assert cadence.cadence_days(plain, {"relationship_stage": "replied"}) == cadence.CADENCE_ACTIVE
    assert cadence.cadence_days(plain, {"relationship_stage": "converted"}) == cadence.CADENCE_ACTIVE
    # one-way / just-met -> loose cadence
    assert cadence.cadence_days(plain, {"relationship_stage": "captured"}) == cadence.CADENCE_LOOSE
    assert cadence.cadence_days(plain, {"relationship_stage": "contacted"}) == cadence.CADENCE_LOOSE


def _stub_reads(monkeypatch, contacts, summ):
    monkeypatch.setattr(cadence.relationships, "list_contacts", lambda db, uid: contacts)
    monkeypatch.setattr(cadence.relationships, "prefetch_interactions_by_prospect",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "prefetch_activity_updates_by_contact",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "contact_summary",
                        lambda db, c, ii, au: summ[c.id])


def test_due_contacts_filters_never_touched_and_ranks(monkeypatch):
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    def days_ago(n):
        return now - timedelta(days=n)

    contacts = [SimpleNamespace(id=i, vip=(i == 1)) for i in (1, 2, 3, 4)]
    summ = {
        # VIP 35d out -> due (ratio 35/30 = 1.17)
        1: {"contact_id": 1, "name": "VIP", "relationship_stage": "replied",
            "last_touch_at": days_ago(35), "next_step": None},
        # active 100d out -> due (ratio 100/90 = 1.11)
        2: {"contact_id": 2, "name": "Active", "relationship_stage": "replied",
            "last_touch_at": days_ago(100), "next_step": None},
        # acquaintance 100d out -> NOT due (cadence 180)
        3: {"contact_id": 3, "name": "Acq", "relationship_stage": "captured",
            "last_touch_at": days_ago(100), "next_step": None},
        # never touched -> skipped entirely
        4: {"contact_id": 4, "name": "New", "relationship_stage": "captured",
            "last_touch_at": None, "next_step": None},
    }
    _stub_reads(monkeypatch, contacts, summ)

    rows = cadence.due_contacts(None, 1, now=now)
    assert [r["contact_id"] for r in rows] == [1, 2]      # VIP outranks active; acq + never-touched out
    assert rows[0]["overdue_ratio"] > rows[1]["overdue_ratio"]
    assert rows[0]["cadence_days"] == cadence.CADENCE_VIP
    assert rows[1]["overdue_days"] == 10                  # 100 - 90

    # lookahead pulls the acquaintance in once she's close enough (100 + 90 >= 180)
    rows2 = cadence.due_contacts(None, 1, now=now, within_days=90)
    assert 3 in [r["contact_id"] for r in rows2]
    assert 4 not in [r["contact_id"] for r in rows2]      # never-touched stays out regardless

    # limit caps the list
    assert len(cadence.due_contacts(None, 1, now=now, limit=1)) == 1


def test_due_contacts_safe_empty_on_read_error(monkeypatch):
    def boom(db, uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(cadence.relationships, "list_contacts", boom)
    assert cadence.due_contacts(None, 1) == []


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend import models
from backend.db import Base


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


def test_snooze_suppresses_then_expires_then_unsnooze(db, monkeypatch):
    # Backdate the host past the touch below: cadence now skips relationships
    # whose last touch predates the host's account (pre-product history is
    # context, not maintenance) unless starred, and this test exercises the
    # SNOOZE mechanics, not that cutoff.
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1",
                    created_at=datetime.now(timezone.utc) - timedelta(days=500))
    db.add(u); db.commit()
    c1 = models.Contact(user_id=u.id, primary_identity_key="li:a", name="A")
    c2 = models.Contact(user_id=u.id, primary_identity_key="li:b", name="B")
    db.add_all([c1, c2]); db.commit()
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    monkeypatch.setattr(cadence.relationships, "list_contacts", lambda db, uid: [c1, c2])
    monkeypatch.setattr(cadence.relationships, "prefetch_interactions_by_prospect",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "prefetch_activity_updates_by_contact",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "contact_summary",
                        lambda db, c, ii, au: {"contact_id": c.id, "name": c.name,
                                               "relationship_stage": "captured",
                                               "last_touch_at": now - timedelta(days=400)})

    def due_ids(at):
        return {r["contact_id"] for r in cadence.due_contacts(db, u.id, now=at)}

    assert due_ids(now) == {c1.id, c2.id}                 # both overdue
    cadence.snooze_contact(db, u.id, c1.id, days=30, now=now)
    assert due_ids(now) == {c2.id}                        # c1 dismissed
    assert due_ids(now + timedelta(days=31)) == {c1.id, c2.id}   # snooze expired -> back
    cadence.snooze_contact(db, u.id, c1.id, days=30, now=now)
    assert cadence.unsnooze_contact(db, u.id, c1.id) is True
    assert due_ids(now) == {c1.id, c2.id}                 # cleared -> back immediately


def test_pre_product_history_is_not_due_unless_vip(db, monkeypatch):
    """Synced history from before the host joined surplus must not surface as
    'overdue for a touch' -- it is context, not a to-do. Starring (VIP) is an
    explicit 'keep this one warm', so it re-enables cadence for that contact."""
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    # Host joined 30 days ago; both touches below predate the account.
    u = models.User(name="Host", email="h2@x.com", unipile_account_id="a2",
                    created_at=now - timedelta(days=30))
    db.add(u); db.commit()
    old = models.Contact(user_id=u.id, primary_identity_key="li:old", name="Old")
    star = models.Contact(user_id=u.id, primary_identity_key="li:star", name="Star",
                          vip=True)
    db.add_all([old, star]); db.commit()
    monkeypatch.setattr(cadence.relationships, "list_contacts", lambda db, uid: [old, star])
    monkeypatch.setattr(cadence.relationships, "prefetch_interactions_by_prospect",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "prefetch_activity_updates_by_contact",
                        lambda db, cs: {})
    monkeypatch.setattr(cadence.relationships, "contact_summary",
                        lambda db, c, ii, au: {"contact_id": c.id, "name": c.name,
                                               "relationship_stage": "captured",
                                               "last_touch_at": now - timedelta(days=400)})
    ids = {r["contact_id"] for r in cadence.due_contacts(db, u.id, now=now)}
    assert old.id not in ids          # pre-product history: skipped
    assert star.id in ids             # VIP: explicitly kept warm

    # A product-era touch re-enables cadence once it goes stale again.
    monkeypatch.setattr(cadence.relationships, "contact_summary",
                        lambda db, c, ii, au: {"contact_id": c.id, "name": c.name,
                                               "relationship_stage": "captured",
                                               "last_touch_at": now - timedelta(days=20)})
    later = now + timedelta(days=200)
    ids = {r["contact_id"] for r in cadence.due_contacts(db, u.id, now=later)}
    assert old.id in ids              # touched after joining -> maintained again
