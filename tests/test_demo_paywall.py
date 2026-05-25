"""
Tests for the not-connected demo + send paywall.

Goal of the feature: a demo session (no connected LinkedIn) can run the
entire workflow, but any real LinkedIn send is gated behind a 402 paywall.

Style mirrors test_triage_routes.py : call route functions directly with an
in-memory session + a real User row, avoiding TestClient/auth plumbing.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, schemas
from backend.auth import (
    require_linkedin_connected,
    require_linkedin_send,         # back-compat alias for any old callers
    require_paid_auto_outreach,
    user_can_send_linkedin,
    user_has_paid,
    user_has_linkedin_connected,
)
from backend.db import Base
from backend.providers import get_preview_provider


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


def _connected_user(db):
    """Paid AND LinkedIn-connected : the only state that can actually send."""
    from datetime import datetime, timezone
    u = models.User(name="Op", email="op@example.com",
                    unipile_account_id="acct_123", linkedin_status="active",
                    paid_at=datetime.now(timezone.utc),
                    stripe_customer_id="cus_test")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _demo_user(db):
    """No payment, no LinkedIn : the demo / free-tier baseline."""
    u = models.User(name="Demo", email="demo@surpluslayer.com",
                    unipile_account_id=None, linkedin_status="disconnected")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _paid_but_unconnected_user(db):
    """Paid, but hasn't connected LinkedIn yet : 402 linkedin_send_locked."""
    from datetime import datetime, timezone
    u = models.User(name="Paying", email="pay@example.com",
                    unipile_account_id=None, linkedin_status="disconnected",
                    paid_at=datetime.now(timezone.utc))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _connected_unpaid_user(db):
    """LinkedIn connected but no payment : 402 payment_required."""
    u = models.User(name="Free", email="free@example.com",
                    unipile_account_id="acct_free", linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _event_with_prospect(db, user):
    ev = models.Event(
        user_id=user.id, role="Infra", seniority="Senior", co_stage="Seed",
        headcount=20, format="Hackathon", city="SF", goal="Hiring pipeline",
        budget=9000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="maya", name="Maya", role="x", company="x",
        seniority="Senior", side="Builds", works_on="x", offers="", seeks="",
        li_resolved=True, linkedin_url="https://www.linkedin.com/in/maya",
        sources="linkedin", fit_score=88, status="approved",
    )
    db.add(p); db.commit()
    return ev, p


# ── capability gate ────────────────────────────────────────────────────

def test_user_can_send_truth_table(db):
    # Only paid + connected can send.
    assert user_can_send_linkedin(_connected_user(db)) is True
    # Free tier (no payment, no LinkedIn) : blocked.
    assert user_can_send_linkedin(_demo_user(db)) is False
    # Paid but not connected : blocked.
    assert user_can_send_linkedin(_paid_but_unconnected_user(db)) is False
    # Connected but not paid : blocked.
    assert user_can_send_linkedin(_connected_unpaid_user(db)) is False
    # Stale LinkedIn (disconnected) : also blocked even if paid.
    from datetime import datetime, timezone
    stale = models.User(name="S", email="s@e.com",
                        unipile_account_id="x", linkedin_status="disconnected",
                        paid_at=datetime.now(timezone.utc))
    assert user_can_send_linkedin(stale) is False


def test_paid_auto_outreach_demo_user_gets_linkedin_locked(db):
    """Free-tier (no payment, no LinkedIn) user hits the LinkedIn paywall
    first : LinkedIn is checked before payment because we don't ask
    people to pay before they've connected the integration."""
    with pytest.raises(HTTPException) as ei:
        require_paid_auto_outreach(_demo_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_paid_auto_outreach_connected_unpaid_user_passes(db):
    """Under the new model payment is collected at connect-LinkedIn time,
    not on send. A user whose row shows connected-but-unpaid (only
    achievable transitionally or via manual DB tweak) can still fire
    sends : the gate that mattered was the connect-LinkedIn one."""
    assert require_paid_auto_outreach(_connected_unpaid_user(db)) is None


def test_require_paid_to_connect_anonymous_passes():
    """Anonymous callers (first-time LinkedIn signup) sail through : we
    don't make them pay before they even have a User row."""
    from backend.auth import require_paid_to_connect_linkedin
    assert require_paid_to_connect_linkedin(None) is None


def test_require_paid_to_connect_unpaid_signed_in_user_gets_402(db):
    """Signed-in unpaid users hit the paywall when they try to attach
    LinkedIn to their existing account."""
    from backend.auth import require_paid_to_connect_linkedin
    with pytest.raises(HTTPException) as ei:
        require_paid_to_connect_linkedin(_demo_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "payment_required"


def test_require_paid_to_connect_paid_user_passes(db):
    """Already-paid users go straight through to the Unipile OAuth start."""
    from backend.auth import require_paid_to_connect_linkedin
    # Use the paid-but-not-yet-connected fixture (just paid, no LinkedIn yet)
    assert require_paid_to_connect_linkedin(_paid_but_unconnected_user(db)) is None


def test_paid_auto_outreach_noop_for_paid_connected_user(db):
    assert require_paid_auto_outreach(_connected_user(db)) is None


# Manual one-off sends (invite/dm) need a connection only, no payment.

def test_linkedin_connected_demo_user_blocked(db):
    """No LinkedIn → 402 linkedin_send_locked regardless of payment."""
    with pytest.raises(HTTPException) as ei:
        require_linkedin_connected(_demo_user(db))
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_linkedin_connected_unpaid_user_passes(db):
    """Connected but UNPAID can fire manual sends : payment is only for
    autonomous batch outreach, not for the mechanical send."""
    assert require_linkedin_connected(_connected_unpaid_user(db)) is None


def test_linkedin_connected_paid_user_passes(db):
    assert require_linkedin_connected(_connected_user(db)) is None


# Back-compat alias still works (forwards to paid_auto_outreach).

def test_require_linkedin_send_alias_demo_user(db):
    with pytest.raises(HTTPException) as ei:
        require_linkedin_send(_demo_user(db))
    assert ei.value.status_code == 402


def test_require_send_noop_for_paid_connected_user(db):
    assert require_linkedin_send(_connected_user(db)) is None


# ── real-send route paywalls the demo user ──────────────────────────────

def test_invite_route_paywalls_demo_user(db):
    from backend.routes.pipeline import send_connection_invite
    user = _demo_user(db)
    ev, p = _event_with_prospect(db, user)
    with pytest.raises(HTTPException) as ei:
        send_connection_invite(ev.id, p.id, schemas.OutreachOverride(), db, user)
    assert ei.value.status_code == 402


def test_dm_route_paywalls_demo_user(db):
    from backend.routes.pipeline import send_direct_message
    user = _demo_user(db)
    ev, p = _event_with_prospect(db, user)
    with pytest.raises(HTTPException) as ei:
        send_direct_message(ev.id, p.id, schemas.OutreachOverride(), db, user)
    assert ei.value.status_code == 402


# ── workflow still works for the demo user ───────────────────────────────

def test_preview_provider_is_dry_run_for_demo_user(db):
    prov = get_preview_provider(_demo_user(db))
    assert prov.dry_run is True
    assert prov.account_id is None


def test_outreach_preview_renders_for_demo_user(db):
    """The compose/preview path must work end-to-end without a LinkedIn
    connection : no 402, no 500, real composed messages come back."""
    from backend.routes.pipeline import outreach_preview
    user = _demo_user(db)
    ev, _p = _event_with_prospect(db, user)
    result = outreach_preview(ev.id, db, user)
    assert result.dry_run is True
    assert result.count_eligible >= 1


# ── demo user bootstrap ──────────────────────────────────────────────────

def test_mint_demo_user_is_not_connected_and_per_visitor(db):
    from backend.routes.demo import _mint_demo_user
    u1 = _mint_demo_user(db)
    assert u1.unipile_account_id is None
    assert user_can_send_linkedin(u1) is False
    u2 = _mint_demo_user(db)
    # Per-visitor : each call is a fresh, isolated demo user (no shared row
    # that could inherit a connection or prior visitor's events).
    assert u2.id != u1.id


# ── /me exposes is_demo so the SPA can hide demo-only surfaces ───────────

def test_me_flags_demo_user_only(db):
    """Demo users (legacy shared row AND per-visitor mints) are is_demo=True;
    a real connected user is False. The SPA keys hiding the ROI ledger stage
    off this flag."""
    import json
    from backend.routes.auth import me
    from backend.routes.demo import _mint_demo_user

    minted = _mint_demo_user(db)
    legacy = models.User(name="Surplus Demo", email="demo@surpluslayer.com")
    db.add(legacy); db.commit(); db.refresh(legacy)
    real = _connected_user(db)

    assert json.loads(me(minted).body)["is_demo"] is True
    assert json.loads(me(legacy).body)["is_demo"] is True
    assert json.loads(me(real).body)["is_demo"] is False
