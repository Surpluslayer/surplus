"""
Tests for the cookie-driven Unipile reconnect path.

PR #52 attempted this with the wrong request body shape (sent
create-only fields like `providers` / `notify_url` / `name` on a
reconnect call); Unipile 4xx'd and blocked sign-in entirely. This file
covers the body-builder helpers in isolation so the shape stays right.

No FastAPI app spin-up : same Python-3.9 workaround test_followups uses.
"""
from __future__ import annotations
from unittest.mock import MagicMock

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
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def _request(cookie_value=None):
    req = MagicMock()
    req.cookies = {"surplus_last_account": cookie_value} if cookie_value else {}
    return req


# ── _create_body shape ────────────────────────────────────────────────

def test_create_body_has_all_create_fields():
    from backend.routes.auth import _create_body
    body = _create_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
    )
    assert body["type"] == "create"
    assert body["providers"] == ["LINKEDIN"]
    assert "notify_url" in body
    assert body["name"] == "state-1"
    assert "success_redirect_url" in body
    assert "failure_redirect_url" in body


# ── _reconnect_body shape (the core PR #52 bug) ───────────────────────

def test_reconnect_body_uses_correct_field_name():
    """Field is `reconnect_account` per Unipile docs."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert body["type"] == "reconnect"
    assert body["reconnect_account"] == "acct-abc"


def test_reconnect_body_omits_create_only_fields():
    """The PR #52 bug : sending `providers` / `notify_url` / `name` on a
    reconnect call made Unipile 4xx. They must NOT be in the body."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert "providers" not in body
    assert "notify_url" not in body
    assert "name" not in body


def test_reconnect_body_keeps_redirect_urls():
    """We still need success_redirect_url so the user comes back to us
    after Unipile re-auths the account."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert "success_redirect_url" in body
    assert "failure_redirect_url" in body


# ── _resolve_returning_user ───────────────────────────────────────────

def test_no_cookie_means_no_returning_user(db):
    from backend.routes.auth import _resolve_returning_user
    assert _resolve_returning_user(_request(None), db) is None


def test_cookie_pointing_at_unknown_account_returns_none(db):
    """Stale cookie (DB reset, user revoked Unipile externally) shouldn't
    crash : caller falls back to create."""
    from backend.routes.auth import _resolve_returning_user
    assert _resolve_returning_user(_request("never-existed"), db) is None


def test_cookie_pointing_at_existing_user_returns_user(db):
    from backend.routes.auth import _resolve_returning_user
    db.add(models.User(unipile_account_id="acct-abc", name="Daniel"))
    db.commit()
    user = _resolve_returning_user(_request("acct-abc"), db)
    assert user is not None
    assert user.unipile_account_id == "acct-abc"


# ── cookie / TTL constants sanity ─────────────────────────────────────

def test_cookie_constant_outlasts_session():
    from backend.auth import (
        LAST_ACCOUNT_COOKIE, LAST_ACCOUNT_TTL_DAYS, SESSION_TTL_DAYS,
    )
    assert LAST_ACCOUNT_COOKIE == "surplus_last_account"
    assert LAST_ACCOUNT_TTL_DAYS > SESSION_TTL_DAYS
