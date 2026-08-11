"""
Tenant scoping for inbound-webhook prospect resolution (audit finding H3).

A LinkedIn member's provider_id is identical across every account that has
interacted with them. When two of our users have both prospected the same
person, a bare global match by provider_lead_id can resolve the webhook to the
WRONG tenant's Prospect, so the accept/reply/auto-DM lands on (and would send
from) the wrong account. These pin that the webhook's account_id scopes the
lookup to the seat that actually received the event.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.providers.base import CanonicalEvent
from backend.routes.webhooks import _resolve_prospect


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


def _user(db, *, name, account_id):
    u = models.User(name=name, unipile_account_id=account_id)
    db.add(u); db.flush()
    return u


def _prospect_for(db, user, *, provider_id):
    ev = models.Event(role="X", seniority="Staff+", co_stage="Seed", headcount=1,
                      format="Dinner", city="SF", goal="G", budget=1, threshold=1,
                      user_id=user.id)
    db.add(ev); db.flush()
    p = models.Prospect(event_id=ev.id, identity=f"id-{user.id}", name="Shared Person",
                        role="Eng", company="Co", seniority="Staff+", side="Builds",
                        works_on="x", offers="x", seeks="x",
                        linkedin_provider_id=provider_id, sources="linkedin",
                        fit_score=80, status="contacted")
    db.add(p); db.flush()
    return p


def _event(state, *, provider_lead_id, account_id):
    return CanonicalEvent(
        event_id=0, prospect_id=0, state=state, provider="unipile",
        provider_lead_id=provider_lead_id, ts=datetime.now(timezone.utc),
        account_id=account_id,
    )


def test_resolves_to_the_account_that_received_the_event(db):
    """Two tenants, same person, same provider_id: the webhook's account_id
    decides which prospect gets the event."""
    a = _user(db, name="Alice", account_id="acct_alice")
    b = _user(db, name="Bob", account_id="acct_bob")
    pa = _prospect_for(db, a, provider_id="ACoAA_shared")
    pb = _prospect_for(db, b, provider_id="ACoAA_shared")
    db.commit()

    got_a = _resolve_prospect(db, _event("message_replied",
                                         provider_lead_id="ACoAA_shared",
                                         account_id="acct_alice"))
    got_b = _resolve_prospect(db, _event("message_replied",
                                         provider_lead_id="ACoAA_shared",
                                         account_id="acct_bob"))
    assert got_a is not None and got_a.id == pa.id
    assert got_b is not None and got_b.id == pb.id
    assert got_a.id != got_b.id


def test_unmapped_account_falls_back_to_global_match(db):
    """Single-tenant / env-operator account (no User owns it): don't drop the
    event, fall back to the legacy global match."""
    a = _user(db, name="Alice", account_id="acct_alice")
    pa = _prospect_for(db, a, provider_id="ACoAA_solo")
    db.commit()

    got = _resolve_prospect(db, _event("invite_accepted",
                                       provider_lead_id="ACoAA_solo",
                                       account_id="env_operator_unmapped"))
    assert got is not None and got.id == pa.id


def test_no_provider_lead_id_returns_none(db):
    assert _resolve_prospect(db, _event("message_replied",
                                        provider_lead_id=None,
                                        account_id="acct_alice")) is None
