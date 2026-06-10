"""Paywall tests for the relationship layer (routes/relationships.py).

Pins that the metered surfaces hard-block at the limit, demo accounts bypass,
and a successful run records usage (per drafted card + contacts scanned). The
agent itself is stubbed — these test the GATE, not the model.

Repo convention: call route functions directly with an in-memory session.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import relationships as rel_route
from backend.agents import relationship_agent as ragent
from backend.agents.relationship_agent import Proposal, RelationshipAgentResult


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


def _user(db, **kw):
    """A user inside a CURRENT billing window (so counters aren't reset by the
    period roll) unless the test overrides the window."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        name="Op", email="op@real.com", unipile_account_id="acct1",
        plan="free",
        billing_period_start=now - timedelta(days=1),
        billing_period_end=now + timedelta(days=29),
    )
    defaults.update(kw)
    u = models.User(**defaults)
    db.add(u); db.commit()
    return u


def _fake_result(*, drafts=0, next_steps=0, contacts_seen=0):
    props = [Proposal(kind="draft_message", contact_id=i, contact_name=f"C{i}",
                      text="hi") for i in range(drafts)]
    props += [Proposal(kind="next_step", contact_id=100 + i, contact_name=f"N{i}",
                       text="do x") for i in range(next_steps)]
    return RelationshipAgentResult(proposals=props, contacts_seen=contacts_seen,
                                   summary="ok")


def _stub_agent(monkeypatch, result):
    def fake_run(db, user_id, **kw):
        return result
    monkeypatch.setattr(ragent, "run_relationship_agent", fake_run)
    monkeypatch.setattr(ragent, "run_relationship_agent_concurrent", fake_run)


# ── hard block at the limit ──────────────────────────────────────────────────

def test_over_draft_limit_returns_402(db, monkeypatch):
    _stub_agent(monkeypatch, _fake_result(drafts=1, contacts_seen=1))
    user = _user(db, drafts_used_this_period=5)  # free limit == 5
    with pytest.raises(HTTPException) as ei:
        rel_route.run_relationship_agent(db=db, user=user)
    assert ei.value.status_code == 402
    assert ei.value.detail["error"] == "LIMIT_REACHED"
    assert ei.value.detail["redirectTo"] == "/billing"


def test_over_contact_limit_returns_402(db, monkeypatch):
    _stub_agent(monkeypatch, _fake_result(drafts=1, contacts_seen=1))
    user = _user(db, drafts_used_this_period=0,
                 contacts_scanned_this_period=25)  # free contacts limit == 25
    with pytest.raises(HTTPException) as ei:
        rel_route.relationship_chat(
            body=rel_route.ChatIn(message="who should I ping?"),
            db=db, user=user)
    assert ei.value.status_code == 402
    assert ei.value.detail["error"] == "CONTACT_LIMIT_REACHED"


# ── demo bypass ──────────────────────────────────────────────────────────────

def test_demo_user_bypasses_limit(db, monkeypatch):
    _stub_agent(monkeypatch, _fake_result(drafts=2, contacts_seen=3))
    user = _user(db, email="visitor@demo.surpluslayer.com",
                 drafts_used_this_period=9999,
                 contacts_scanned_this_period=9999)
    out = rel_route.run_relationship_agent(db=db, user=user)  # no raise
    assert out["summary"] == "ok"


# ── usage recorded after a successful run ────────────────────────────────────

def test_usage_recorded_per_card_and_per_contact(db, monkeypatch):
    # 2 drafts + 1 next_step => only the 2 DRAFT cards count; 7 contacts scanned.
    _stub_agent(monkeypatch, _fake_result(drafts=2, next_steps=1, contacts_seen=7))
    user = _user(db, drafts_used_this_period=0, contacts_scanned_this_period=0)
    rel_route.run_relationship_agent(db=db, user=user)
    db.refresh(user)
    assert user.drafts_used_this_period == 2
    assert user.contacts_scanned_this_period == 7


def test_usage_accumulates_across_runs_then_blocks(db, monkeypatch):
    _stub_agent(monkeypatch, _fake_result(drafts=3, contacts_seen=2))
    user = _user(db, drafts_used_this_period=0)
    rel_route.run_relationship_agent(db=db, user=user)   # -> 3 drafts
    rel_route.run_relationship_agent(db=db, user=user)   # -> 6 drafts (>=5)
    db.refresh(user)
    assert user.drafts_used_this_period == 6
    # now over the free draft cap -> next call blocks
    with pytest.raises(HTTPException) as ei:
        rel_route.run_relationship_agent(db=db, user=user)
    assert ei.value.detail["error"] == "LIMIT_REACHED"


# ── period roll clears prior usage ───────────────────────────────────────────

def test_elapsed_period_resets_then_allows(db, monkeypatch):
    _stub_agent(monkeypatch, _fake_result(drafts=1, contacts_seen=1))
    now = datetime.now(timezone.utc)
    user = _user(db, drafts_used_this_period=5,            # was over the cap
                 billing_period_start=now - timedelta(days=40),
                 billing_period_end=now - timedelta(days=10))  # window elapsed
    out = rel_route.run_relationship_agent(db=db, user=user)  # roll resets to 0
    db.refresh(user)
    assert out["summary"] == "ok"
    assert user.drafts_used_this_period == 1  # reset to 0, then +1 for this run
