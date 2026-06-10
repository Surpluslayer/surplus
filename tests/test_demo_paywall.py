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
    require_can_send_linkedin,
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


# Unified send gate : every real send (manual invite/dm AND batch outreach)
# requires BOTH a connected LinkedIn AND a paid Stripe subscription. The
# whole workflow up to the send is free (demo-like); Stripe is the one paywall.

def test_send_gate_demo_user_gets_linkedin_locked(db):
    """Free-tier (no payment, no LinkedIn) user hits the LinkedIn paywall
    first : LinkedIn is checked before payment so a user with neither is
    asked to connect before being asked to pay."""
    with pytest.raises(HTTPException) as ei:
        require_can_send_linkedin(_demo_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_send_gate_connected_unpaid_user_gets_payment_required(db):
    """Connected but UNPAID : Stripe is the paywall, so the send is blocked
    with payment_required (frontend opens Stripe Checkout)."""
    with pytest.raises(HTTPException) as ei:
        require_can_send_linkedin(_connected_unpaid_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "payment_required"


def test_send_gate_paid_but_unconnected_user_gets_linkedin_locked(db):
    """Paid but no LinkedIn yet : asked to connect first."""
    with pytest.raises(HTTPException) as ei:
        require_can_send_linkedin(_paid_but_unconnected_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_send_gate_noop_for_paid_connected_user(db):
    """Signed in on LinkedIn AND paid Stripe : sends freely, no paywall."""
    assert require_can_send_linkedin(_connected_user(db)) is None


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


# ── demo entry lands on the chosen surface ───────────────────────────────

def test_demo_enter_surface_routing(db, monkeypatch):
    """A demo link can target a surface : ?surface=book lands on /book, the
    advisor "Your book today" page. Omitted / unknown / bad values fall back
    to "/" (the desktop pipeline) so the value can't become an open redirect."""
    from backend.routes import demo as demo_mod

    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "s3cret")

    def _enter(surface):
        resp = demo_mod.demo_enter(key="s3cret", surface=surface, db=db)
        assert resp.status_code == 303
        return resp.headers["location"]

    assert _enter("book") == "/book"
    assert _enter("inperson") == "/inperson"
    assert _enter("app") == "/"
    # default + unknown both fall back to the desktop pipeline (no open redirect)
    assert _enter(None) == "/"
    assert _enter("https://evil.example.com") == "/"
    assert _enter("BOOK") == "/book"  # case-insensitive


def test_demo_enter_default_surface_is_env_configurable(db, monkeypatch):
    """DEMO_DEFAULT_SURFACE sets where an omitted ?surface= lands, per env, so
    e.g. staging can default to book while production keeps the desktop app.
    An explicit surface still wins; an unknown env value falls back to "app"."""
    from backend.routes import demo as demo_mod

    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "s3cret")
    monkeypatch.setenv("DEMO_DEFAULT_SURFACE", "book")

    def _enter(surface):
        resp = demo_mod.demo_enter(key="s3cret", surface=surface, db=db)
        return resp.headers["location"]

    # Omitted / unknown now land on the configured default (book), not "/".
    assert _enter(None) == "/book"
    assert _enter("https://evil.example.com") == "/book"
    # An explicit surface still overrides the default.
    assert _enter("inperson") == "/inperson"
    assert _enter("app") == "/"
    # A bogus default value is ignored (back to the desktop pipeline).
    monkeypatch.setenv("DEMO_DEFAULT_SURFACE", "nonsense")
    assert _enter(None) == "/"


def test_demo_enter_bad_key_is_404_regardless_of_surface(db, monkeypatch):
    """Surface never bypasses the token gate : a wrong key is still 404."""
    from backend.routes import demo as demo_mod

    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "s3cret")
    resp = demo_mod.demo_enter(key="wrong", surface="book", db=db)
    assert resp.status_code == 404


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
