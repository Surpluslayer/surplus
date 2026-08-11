"""Tests for the REAL wiring of the Rule 7.3 gate: real User/Contact rows,
real RelationshipInteraction send history, through
agents/relationship/solicitation_signals.py and
pipeline/send/sender.automated_send_allowed_for_contact.

This is the integration layer solicitation_harness.py explicitly does NOT
cover (that harness calls solicitation.evaluate() directly, in isolation).
These tests exercise the actual DB-backed path a caller hits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.agents.relationship import solicitation_signals as sig
from backend.agents.relationship.pipeline.send import sender


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _make_user(db, *, bar_jurisdiction=None):
    u = models.User(email="lawyer@example.com", bar_jurisdiction=bar_jurisdiction)
    db.add(u)
    db.commit()
    return u


def _make_contact(db, user, *, relationship_type=None):
    c = models.Contact(
        user_id=user.id, primary_identity_key="li:test-contact",
        name="Jane Prospect", relationship_type=relationship_type,
    )
    db.add(c)
    db.commit()
    return c


def _touch(db, user, contact, *, direction="outbound", occurred_at=None):
    i = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        source_type="message", interaction_type="dm", direction=direction,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    db.add(i)
    db.commit()
    return i


# ── resolve_context / check ────────────────────────────────────────────────

def test_unset_relationship_type_resolves_to_prospect(db):
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user)  # relationship_type unset
    verdict = sig.check(db, user, contact, "phone_call")
    assert not verdict.allowed
    assert "real-time" in verdict.reason


def test_existing_client_is_exempt_via_real_row(db):
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user, relationship_type="existing_client")
    verdict = sig.check(db, user, contact, "phone_call")
    assert verdict.allowed
    assert "exempt" in verdict.reason


def test_unrecognized_relationship_type_value_fails_closed(db):
    # Simulates bad/legacy data in the column -- must not silently exempt.
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user, relationship_type="not_a_real_category")
    verdict = sig.check(db, user, contact, "phone_call")
    assert not verdict.allowed


def test_unset_bar_jurisdiction_fails_closed(db):
    user = _make_user(db, bar_jurisdiction=None)
    contact = _make_contact(db, user)
    verdict = sig.check(db, user, contact, "email")
    # written channel still permitted, but under the fail-closed default rule
    assert verdict.allowed
    assert verdict.requires_disclosure_label


def test_volume_count_reads_real_send_history(db):
    user = _make_user(db, bar_jurisdiction="FL")
    contact = _make_contact(db, user)
    _touch(db, user, contact, direction="outbound")  # 1 real prior send
    verdict = sig.check(db, user, contact, "email")
    # FL's cap is (1, 30) -- one prior send already at the cap
    assert not verdict.allowed
    assert "caps solicitation" in verdict.reason


def test_volume_count_ignores_inbound_and_stale_touches(db):
    user = _make_user(db, bar_jurisdiction="FL")
    contact = _make_contact(db, user)
    _touch(db, user, contact, direction="inbound")  # doesn't count
    _touch(db, user, contact, direction="outbound",
           occurred_at=datetime.now(timezone.utc) - timedelta(days=45))  # outside window
    verdict = sig.check(db, user, contact, "email")
    assert verdict.allowed  # zero counted touches in-window -> under the cap


# ── sender.automated_send_allowed_for_contact: needs BOTH gates ────────────

def test_blocked_when_automation_master_is_off(db, monkeypatch):
    monkeypatch.delenv("SURPLUS_AUTOMATED_SENDS", raising=False)  # default off
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user, relationship_type="existing_client")
    # Exempt relationship, would pass the solicitation gate -- but the
    # automation master switch is off, so it must still be blocked.
    assert sender.automated_send_allowed_for_contact(db, user, contact, "email") is False


def test_allowed_when_master_on_and_solicitation_gate_passes(db, monkeypatch):
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user, relationship_type="existing_client")
    assert sender.automated_send_allowed_for_contact(db, user, contact, "email") is True


def test_blocked_when_master_on_but_solicitation_gate_fails(db, monkeypatch):
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    user = _make_user(db, bar_jurisdiction="NY")
    contact = _make_contact(db, user)  # unset -> prospect
    assert sender.automated_send_allowed_for_contact(db, user, contact, "phone_call") is False
