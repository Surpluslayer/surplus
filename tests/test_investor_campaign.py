"""
Tests for the batched investor connection-request campaign
(backend/agents/relationship/investor_campaign.py).

All dry-run (UNIPILE_DRY_RUN=true), so nothing touches the network: the goal is
to prove the seeding is idempotent, the confidence gate and daily cap hold, and
sends route through the guarded path and log correctly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.providers import reset_provider_cache
from backend.agents.relationship import investor_campaign as ic


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_DSN", "https://example.invalid")
    monkeypatch.setenv("UNIPILE_API_KEY", "test")
    monkeypatch.setenv("INVESTOR_OUTREACH_USER_EMAIL", "founder@example.com")
    reset_provider_cache()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        reset_provider_cache()


def _user(db):
    u = models.User(email="founder@example.com",
                    unipile_account_id="acct_123", linkedin_status="active")
    db.add(u)
    db.commit()
    return u


def test_roster_notes_within_linkedin_limit():
    roster = ic.load_roster()
    assert len(roster) >= 50
    assert all(len(r["note"]) <= 300 for r in roster)
    # identities are unique (idempotency key)
    idents = [r["identity"] for r in roster]
    assert len(idents) == len(set(idents))


def test_seed_is_idempotent(db):
    u = _user(db)
    ev, created = ic.seed_roster_event(db, u)
    n = len(ic.load_roster())
    assert created == n
    assert len(ev.prospects) == n

    ev2, created2 = ic.seed_roster_event(db, u)
    assert created2 == 0                 # no duplicates on re-seed
    assert ev2.id == ev.id
    assert len(ev2.prospects) == n


def test_confidence_gate_holds_back_ambiguous_rows(db):
    u = _user(db)
    ev, _ = ic.seed_roster_event(db, u)
    high = ic.pending_count(db, ev, high_only=True)
    allc = ic.pending_count(db, ev, high_only=False)
    n_high = sum(1 for r in ic.load_roster() if r["confidence"] == "high")
    assert high == n_high
    assert allc == len(ic.load_roster())
    assert allc > high                    # some rows are deliberately held back


def test_dry_run_batch_sends_nothing_and_respects_cap(db):
    u = _user(db)
    summary = ic.run_batch(db, user=u, limit=5, high_only=True)
    assert summary["dry_run"] is True
    assert summary["attempted"] == 5
    assert summary["sent"] == 0           # dry-run never sends
    # every attempt logged as a dry-run row on the cold path
    logs = db.query(models.OutreachLog).all()
    assert len(logs) == 5
    assert all(g.state == "dry_run_queued" for g in logs)


def test_resolve_sender_requires_connected_account(db):
    # a user with no LinkedIn connection is not a valid sender
    db.add(models.User(email="founder@example.com"))
    db.commit()
    with pytest.raises(LookupError):
        ic.resolve_sender_user(db)
