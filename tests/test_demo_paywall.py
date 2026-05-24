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
from backend.auth import require_linkedin_send, user_can_send_linkedin
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
    u = models.User(name="Op", email="op@example.com",
                    unipile_account_id="acct_123", linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _demo_user(db):
    u = models.User(name="Demo", email="demo@surpluslayer.com",
                    unipile_account_id=None, linkedin_status="disconnected")
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
    assert user_can_send_linkedin(_connected_user(db)) is True
    assert user_can_send_linkedin(_demo_user(db)) is False
    # connected account but stale connection : also blocked
    stale = models.User(name="S", email="s@e.com",
                        unipile_account_id="x", linkedin_status="disconnected")
    assert user_can_send_linkedin(stale) is False


def test_require_send_raises_402_for_demo_user(db):
    with pytest.raises(HTTPException) as ei:
        require_linkedin_send(_demo_user(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_require_send_noop_for_connected_user(db):
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

def test_get_or_create_demo_user_is_not_connected_and_idempotent(db):
    from backend.routes.demo import _get_or_create_demo_user
    u1 = _get_or_create_demo_user(db)
    assert u1.unipile_account_id is None
    assert user_can_send_linkedin(u1) is False
    u2 = _get_or_create_demo_user(db)
    assert u2.id == u1.id  # idempotent : one shared demo user
