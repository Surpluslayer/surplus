"""
Tests for the first-time-user onboarding tour state.

Covers the two server-side pieces the in-person coachmark flow depends on:
  1. `_arm_onboarding_if_first_connect` : armed exactly once, the instant a
     user first gains a LinkedIn connection (gated on the empty default so a
     re-connect / profile refresh never re-arms or re-shows the tour).
  2. `PUT /api/auth/onboarding` (update_onboarding) : persists progress so the
     tour resumes in place and can be finished / skipped / replayed.

Follows the repo convention : call the route functions directly with an
in-memory SQLAlchemy session + real User rows (no TestClient/auth cookies).
"""
from __future__ import annotations

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
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name="Op", email="op@example.com", **kw)
    db.add(u); db.commit()
    return u


# ── arm-on-first-connect ────────────────────────────────────────────────────

def test_arm_sets_active_for_fresh_user():
    from backend.routes.auth import _arm_onboarding_if_first_connect
    u = models.User(name="New", unipile_account_id="acct_1")  # status defaults ""
    assert (u.onboarding_status or "") == ""
    _arm_onboarding_if_first_connect(u)
    assert u.onboarding_status == "active"
    assert u.onboarding_step == 0


@pytest.mark.parametrize("status", ["active", "done", "skipped"])
def test_arm_is_noop_once_status_set(status):
    """A re-connect / profile refresh must never re-arm the tour : the gate is
    the empty default, so any non-empty status is left untouched."""
    from backend.routes.auth import _arm_onboarding_if_first_connect
    u = models.User(name="Returning", unipile_account_id="acct_1",
                    onboarding_status=status, onboarding_step=4)
    _arm_onboarding_if_first_connect(u)
    assert u.onboarding_status == status
    assert u.onboarding_step == 4


# ── progress endpoint ───────────────────────────────────────────────────────

def test_update_onboarding_advances_step(db):
    from backend.routes.auth import update_onboarding, OnboardingPatch
    u = _user(db, unipile_account_id="acct_1", onboarding_status="active")
    import json
    out = json.loads(update_onboarding(OnboardingPatch(step=3), db, u).body)
    assert out["onboarding_step"] == 3
    assert out["onboarding_status"] == "active"
    db.refresh(u)
    assert u.onboarding_step == 3


def test_update_onboarding_finish_and_skip(db):
    from backend.routes.auth import update_onboarding, OnboardingPatch
    import json
    u = _user(db, unipile_account_id="acct_1", onboarding_status="active",
              onboarding_step=2)
    update_onboarding(OnboardingPatch(status="done"), db, u)
    db.refresh(u)
    assert u.onboarding_status == "done"
    # Skipping flips status without disturbing the recorded step.
    update_onboarding(OnboardingPatch(status="skipped"), db, u)
    db.refresh(u)
    assert u.onboarding_status == "skipped"


def test_update_onboarding_replay_resets_step(db):
    from backend.routes.auth import update_onboarding, OnboardingPatch
    u = _user(db, unipile_account_id="acct_1", onboarding_status="skipped",
              onboarding_step=5)
    # "Replay tour" from settings : status active with no explicit step rewinds.
    update_onboarding(OnboardingPatch(status="active"), db, u)
    db.refresh(u)
    assert u.onboarding_status == "active"
    assert u.onboarding_step == 0


def test_update_onboarding_ignores_bogus_status(db):
    from backend.routes.auth import update_onboarding, OnboardingPatch
    u = _user(db, unipile_account_id="acct_1", onboarding_status="active",
              onboarding_step=1)
    update_onboarding(OnboardingPatch(status="banana"), db, u)
    db.refresh(u)
    assert u.onboarding_status == "active"   # unchanged
