"""
Tests for the cookie-driven Unipile reconnect path : prevents the bug
where every sign-in created a fresh Unipile account (and billed seat).

Covers only the body-building helper (_build_hosted_auth_body) so we
don't have to spin up the full FastAPI app : same workaround test_followups
uses to avoid the Python 3.9 / str | None evaluation issue on schemas.
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


def _request_with_cookie(value: str | None) -> MagicMock:
    req = MagicMock()
    req.cookies = {"surplus_last_account": value} if value else {}
    return req


def test_no_cookie_means_create(db):
    from backend.routes.auth import _build_hosted_auth_body
    body = _build_hosted_auth_body(
        request=_request_with_cookie(None),
        db=db,
        state_token="state-123",
        base="https://www.surpluslayer.com",
        dsn="https://api.example/api",
    )
    assert body["type"] == "create"
    assert "reconnect_account" not in body


def test_cookie_pointing_at_unknown_account_falls_back_to_create(db):
    """If the cookie value doesn't match any User row, we can't reconnect
    safely : fall through to create. (Could happen if the DB was reset
    or the user revoked the underlying Unipile account externally.)"""
    from backend.routes.auth import _build_hosted_auth_body
    body = _build_hosted_auth_body(
        request=_request_with_cookie("nonexistent-account-id"),
        db=db,
        state_token="state-123",
        base="https://www.surpluslayer.com",
        dsn="https://api.example/api",
    )
    assert body["type"] == "create"


def test_cookie_pointing_at_existing_user_uses_reconnect(db):
    """Happy path : returning user on same browser, cookie matches a User
    row in our DB, so the hosted-auth call is reconnect (not create)."""
    db.add(models.User(unipile_account_id="acct-abc", name="Daniel"))
    db.commit()

    from backend.routes.auth import _build_hosted_auth_body
    body = _build_hosted_auth_body(
        request=_request_with_cookie("acct-abc"),
        db=db,
        state_token="state-xyz",
        base="https://www.surpluslayer.com",
        dsn="https://api.example/api",
    )
    assert body["type"] == "reconnect"
    assert body["reconnect_account"] == "acct-abc"


def test_reconnect_preserves_common_fields(db):
    """Reconnect must still carry the same redirect / webhook URLs as
    create : we'd never see the response otherwise."""
    db.add(models.User(unipile_account_id="acct-abc", name="Daniel"))
    db.commit()

    from backend.routes.auth import _build_hosted_auth_body
    body = _build_hosted_auth_body(
        request=_request_with_cookie("acct-abc"),
        db=db,
        state_token="state-xyz",
        base="https://www.surpluslayer.com",
        dsn="https://api.example/api",
    )
    assert "success_redirect_url" in body
    assert "failure_redirect_url" in body
    assert "notify_url" in body
    assert body["name"] == "state-xyz"
    assert "LINKEDIN" in body["providers"]


def test_cookie_constant_and_ttl_make_sense():
    """Sanity check : cookie outlasts the session cookie so an expired
    session still gets a frictionless return."""
    from backend.auth import (
        LAST_ACCOUNT_COOKIE, LAST_ACCOUNT_TTL_DAYS, SESSION_TTL_DAYS,
    )
    assert LAST_ACCOUNT_COOKIE == "surplus_last_account"
    assert LAST_ACCOUNT_TTL_DAYS > SESSION_TTL_DAYS
