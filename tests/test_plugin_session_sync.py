"""
Tests for the extension session-sync flow:

  POST /api/auth/plugin/token   -> mint a client="plugin" Bearer token
  GET  /api/auth/token-bootstrap -> adopt a valid token into the first-party
                                    session cookie (for the partitioned Book
                                    iframe), 303 to a same-origin `next`.

These pin the contract the Chrome extension relies on to make its
service-worker calls AND the embedded Book iframe resolve to the SAME account.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db, SessionLocal
from backend.main import app
from backend import models
from backend.auth import SESSION_COOKIE, create_session


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db()
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _seed_user_with_session(client="web"):
    db = SessionLocal()
    try:
        user = models.User(
            name="Maya", email="maya@example.com",
            unipile_account_id="acct_1", linkedin_status="active",
        )
        db.add(user)
        db.flush()
        sess = create_session(db, user, client=client)
        return user.id, sess.session_token
    finally:
        db.close()


def test_plugin_token_requires_auth(client):
    """No session -> 401. The endpoint never mints a token for an anonymous
    caller (it only re-issues for the user you're already signed in as)."""
    r = client.post("/api/auth/plugin/token")
    assert r.status_code == 401


def test_plugin_token_mints_plugin_session_via_cookie(client):
    """A cookie-authenticated caller gets back a NEW client='plugin' token
    that resolves to the same user."""
    user_id, web_token = _seed_user_with_session(client="web")
    r = client.post("/api/auth/plugin/token",
                    cookies={SESSION_COOKIE: web_token})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == user_id
    token = body["token"]
    assert token and token != web_token

    db = SessionLocal()
    try:
        sess = db.query(models.Session).filter_by(session_token=token).first()
        assert sess is not None
        assert sess.client == "plugin"
        assert sess.user_id == user_id
    finally:
        db.close()


def test_plugin_token_works_via_bearer(client):
    """A Bearer-authenticated caller (e.g. re-minting from an existing plugin
    token) also works -- the current_user dependency accepts either transport."""
    user_id, token = _seed_user_with_session(client="plugin")
    r = client.post("/api/auth/plugin/token",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == user_id


def test_token_bootstrap_sets_cookie_for_valid_token(client):
    """The iframe entry point: a valid token gets mirrored into the first-party
    session cookie and 303s to the same-origin next."""
    _user_id, token = _seed_user_with_session(client="plugin")
    r = client.get(f"/api/auth/token-bootstrap?token={token}&next=/")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie
    assert token in set_cookie


def test_token_bootstrap_cookie_is_partition_friendly(client, monkeypatch):
    """Over HTTPS the bootstrap cookie must be CHIPS-partitioned so Chrome
    actually STORES it inside the third-party iframe: Secure + SameSite=None +
    Partitioned. (Plain http dev keeps the Lax cookie -- not asserted here.)"""
    monkeypatch.setenv("SURPLUS_SESSION_COOKIE_SECURE", "1")
    _user_id, token = _seed_user_with_session(client="plugin")
    r = client.get(f"/api/auth/token-bootstrap?token={token}&next=/")
    set_cookie = r.headers.get("set-cookie", "")
    low = set_cookie.lower()
    assert "secure" in low
    assert "samesite=none" in low
    assert "partitioned" in low


def test_token_bootstrap_ignores_bad_token(client):
    """A bad/expired/revoked token sets NO cookie but still bounces to next so
    the SPA falls through to its sign-in screen (never 500s)."""
    r = client.get("/api/auth/token-bootstrap?token=not-a-real-token&next=/")
    assert r.status_code == 303
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")


def test_token_bootstrap_rejects_open_redirect(client):
    """`next` is constrained to a same-origin path: an absolute or
    protocol-relative URL is coerced to '/' so this can't be an open redirect."""
    _user_id, token = _seed_user_with_session(client="plugin")
    for bad in ("https://evil.example.com/", "//evil.example.com/"):
        r = client.get(f"/api/auth/token-bootstrap?token={token}&next={bad}")
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_logout_revokes_bearer_plugin_session(client):
    """Extension sign-out: logout sent with the plugin Bearer token must revoke
    THAT session (the partitioned cookie jar may not carry the cookie)."""
    _user_id, token = _seed_user_with_session(client="plugin")
    r = client.post("/api/auth/logout",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        sess = db.query(models.Session).filter_by(session_token=token).first()
        assert sess is not None and sess.revoked_at is not None
    finally:
        db.close()
