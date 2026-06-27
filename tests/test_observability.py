"""Tests for the deterministic-layer status snapshot
(agents/relationship/observability.py). Real in-memory DB; proactive.collect_due
is stubbed so we test the fact-coverage math + assembly."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.spine import memory as cm
from backend.agents.relationship import observability


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


def _seed(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c1 = models.Contact(user_id=u.id, primary_identity_key="li:a", name="A")
    c2 = models.Contact(user_id=u.id, primary_identity_key="li:b", name="B")
    db.add_all([c1, c2]); db.commit()
    cm.upsert_fact(db, u.id, c1.id, "based_in", "NYC", source="linkedin")
    cm.upsert_fact(db, u.id, c1.id, "birthday", "03-04", source="manual")
    # c2 has no facts -> coverage is 1 of 2
    return u, c1, c2


def test_fact_stats_coverage_and_breakdown(db):
    u, c1, c2 = _seed(db)
    s = observability._fact_stats(db, u.id)
    assert s["total_facts"] == 2
    assert s["contacts_with_facts"] == 1 and s["total_contacts"] == 2
    assert s["coverage_pct"] == 50.0
    assert s["by_key"] == {"based_in": 1, "birthday": 1}
    assert s["by_source"]["linkedin"] == 1 and s["by_source"]["manual"] == 1


def test_relationship_status_assembles_sections(db, monkeypatch):
    u, _c1, _c2 = _seed(db)
    monkeypatch.setattr(observability.proactive, "collect_due",
                        lambda db, uid, **k: {"counts": {"contacts": 3, "triggers": 1}})
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    monkeypatch.setenv("SURPLUS_AUTOMATED_SEND_CHANNELS", "whatsapp,email")
    st = observability.relationship_status(db, u.id)
    assert st["facts"]["total_facts"] == 2
    assert st["due"] == {"contacts": 3, "triggers": 1}
    assert st["automation"]["master_on"] is True
    assert st["automation"]["channels"] == ["email", "whatsapp"]
    assert set(st["schedulers"]) == {"updates", "proactive"}
    assert st["sends"]["total"] == 0          # no outreach seeded


def test_send_outcomes_counts_and_failure_rate(db):
    from datetime import datetime, timezone
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    ev = models.Event(user_id=u.id, city="NYC")
    db.add(ev); db.commit()
    p = models.Prospect(event_id=ev.id, identity="x", name="X")
    db.add(p); db.commit()
    for state in ("message_sent", "follow_up_sent", "failed"):
        db.add(models.OutreachLog(prospect_id=p.id, channel="linkedin",
                                  state=state, ts=datetime.now(timezone.utc)))
    db.commit()
    out = observability._send_outcomes(db, u.id)
    assert out["total"] == 3
    assert out["by_state"]["message_sent"] == 1
    assert out["failure_rate_pct"] == 33.3     # 1 failed of 3
