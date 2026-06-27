"""Tests for the proactive surface (agents/relationship/proactive.py): the unified
'what's due' collector + the sweep. Real in-memory DB for the dated-fact store;
cadence is stubbed so we exercise the unification + the consume/fire semantics."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import contact_memory as cm, proactive


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


def _contact(db, name="Sarah", key="li:sarah"):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key=key, name=name)
    db.add(c); db.commit()
    return u, c


NOW = datetime(2026, 6, 27, tzinfo=timezone.utc)


def test_collect_due_combines_cadence_and_triggers(db, monkeypatch):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "birthday", "cake", due_date=NOW, recurring=True)
    monkeypatch.setattr(proactive.cadence, "due_contacts",
                        lambda db, uid, **k: [{"contact_id": c.id, "name": "Sarah",
                                               "overdue_ratio": 1.2}])
    snap = proactive.collect_due(db, u.id, now=NOW)
    assert snap["counts"] == {"contacts": 1, "triggers": 1}
    t = snap["triggers_due"][0]
    assert t["key"] == "birthday" and t["name"] == "Sarah"   # contact resolved
    assert snap["contacts_due"][0]["contact_id"] == c.id


def test_sweep_collect_only_consumes_nothing(db, monkeypatch):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "birthday", "cake", due_date=NOW, recurring=True)
    monkeypatch.setattr(proactive.cadence, "due_contacts", lambda db, uid, **k: [])
    res = proactive.run_proactive_sweep(db, now=NOW)          # on_due None -> read-only
    assert res["fired"] is False and res["triggers_due"] == 1
    # NOT marked fired -- still due on a later read
    assert len(cm.due_facts(db, now=NOW, user_id=u.id)) == 1


def test_sweep_with_on_due_fires_and_consumes(db, monkeypatch):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "flight", "SFO", due_date=NOW, dedup_key="f1")  # one-off
    monkeypatch.setattr(proactive.cadence, "due_contacts", lambda db, uid, **k: [])
    seen = []
    res = proactive.run_proactive_sweep(db, now=NOW,
                                        on_due=lambda f, ct: seen.append((f.key, ct.id)))
    assert res["fired"] is True and res["triggers_due"] == 1
    assert seen == [("flight", c.id)]                         # callback got the contact
    assert cm.get_facts(db, c.id, key="flight") == []        # one-off consumed


def test_daily_plan_dedupes_trigger_over_cadence(monkeypatch):
    """The plan merges both sources: a trigger outranks cadence, a contact due for
    BOTH appears once (as the trigger), and cadence-only contacts rank by overdue."""
    snap = {
        "triggers_due": [{"contact_id": 1, "name": "A", "key": "birthday", "value": ""}],
        "contacts_due": [
            {"contact_id": 1, "name": "A", "reason": "stale A", "overdue_ratio": 2.0},  # dup
            {"contact_id": 2, "name": "B", "reason": "stale B", "overdue_ratio": 1.5},
            {"contact_id": 3, "name": "C", "reason": "stale C", "overdue_ratio": 1.1},
        ],
        "counts": {},
    }
    monkeypatch.setattr(proactive, "collect_due", lambda db, uid, **k: snap)
    out = proactive.daily_plan(None, 1)
    plan = out["plan"]
    assert [p["contact_id"] for p in plan] == [1, 2, 3]       # trigger first, then by overdue desc
    assert plan[0]["kind"] == "trigger" and "birthday" in plan[0]["reason"]
    assert sum(1 for p in plan if p["contact_id"] == 1) == 1  # contact 1 deduped
    assert plan[1]["kind"] == "cadence" and out["count"] == 3
