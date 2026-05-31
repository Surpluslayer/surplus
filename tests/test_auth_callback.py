"""
Regression tests for the LinkedIn auth callback (GET /api/auth/linkedin/callback)
over the real ASGI app.

This is the integration seam the direct-function unit tests miss : the callback
takes its params via FastAPI dependency injection (Request + Query), so a missing
`request` parameter only blows up when routed through the app. PR #191 added a
`request_browser_host(request)` call to the callback without adding the param,
500ing every sign-in : these tests pin that down.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db, SessionLocal
from backend.main import app
from backend import models


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    # No real Unipile : the webhook lands the user, the callback just resolves.
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    reset_db()
    yield


@pytest.fixture
def client():
    # Don't follow the 303 : we want to inspect the redirect itself, and the
    # Location may point at an absolute event.surpluslayer.com URL.
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _seed_completed_auth(account_id="acct_1", state="state_abc"):
    """Mimic the webhook having already upserted the user + marked the
    AuthState done, which is the state the browser callback expects."""
    db = SessionLocal()
    try:
        user = models.User(
            name="Maya", email="maya@example.com",
            unipile_account_id=account_id, linkedin_status="active",
        )
        db.add(user); db.flush()
        st = models.AuthState(state_token=state, status="webhook_done",
                              user_id=user.id)
        db.add(st); db.commit()
        return user.id
    finally:
        db.close()


def test_callback_does_not_500_and_sets_session(client):
    """The core regression : the callback must not raise (it used to NameError
    on an undeclared `request`). It should 303 + set the session cookie."""
    _seed_completed_auth(state="state_abc")
    r = client.get("/api/auth/linkedin/callback?state=state_abc&account_id=acct_1")
    assert r.status_code == 303
    assert "surplus_session" in r.headers.get("set-cookie", "")


def test_callback_returns_to_event_host_when_flow_began_there(client):
    """When the request arrives on the in-person host, the post-auth redirect
    should land back on event.surpluslayer.com, not the apex."""
    _seed_completed_auth(state="state_evt", account_id="acct_evt")
    r = client.get(
        "/api/auth/linkedin/callback?state=state_evt&account_id=acct_evt",
        headers={"host": "event.surpluslayer.com",
                 "origin": "https://event.surpluslayer.com"},
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://event.surpluslayer.com/")


def test_callback_unknown_state_redirects_not_500(client):
    r = client.get("/api/auth/linkedin/callback?state=nope")
    assert r.status_code == 303
    assert "error=linkedin_callback_failed" in r.headers["location"]


def test_callback_on_event_host_sets_shared_domain_cookie(client, monkeypatch):
    """The 'bounced back to login' bug: on event.surpluslayer.com the callback
    must set the session cookie with Domain=.surpluslayer.com (auto-derived from
    the host, no env var) so it survives the next request instead of being
    dropped as host-only."""
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    _seed_completed_auth(state="state_dom", account_id="acct_dom")
    r = client.get(
        "/api/auth/linkedin/callback?state=state_dom&account_id=acct_dom",
        headers={"host": "event.surpluslayer.com",
                 "origin": "https://event.surpluslayer.com"},
    )
    assert r.status_code == 303
    sc = r.headers.get("set-cookie", "")
    assert "surplus_session=" in sc
    assert "Domain=.surpluslayer.com" in sc


def test_session_recognized_after_callback_round_trip(monkeypatch):
    """The full symptom: after the callback sets the cookie on the event host,
    the SPA's very next api.me() must recognize the session (200), not 401 ->
    bounce back to the login screen. Uses a real event.surpluslayer.com base_url
    so the cookie jar enforces the Domain exactly like a browser would : a
    host-only cookie (the bug) would NOT be re-sent and me() would 401."""
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    uid = _seed_completed_auth(state="state_rt", account_id="acct_rt")
    c = TestClient(app, base_url="https://event.surpluslayer.com",
                   follow_redirects=False)
    r = c.get("/api/auth/linkedin/callback?state=state_rt&account_id=acct_rt")
    assert r.status_code == 303
    me = c.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["id"] == uid
