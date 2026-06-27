"""Tests for the OAuth / connected-account framework (backend/integrations). No live
Google: httpx is mocked. Covers signed-state CSRF, config gating, authorize URL,
code exchange + refresh, token storage (refresh-token preservation), and the
'valid access token' refresh-on-expiry path."""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import oauth


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


# ── signed state ──────────────────────────────────────────────────────────────
def test_state_roundtrip_tamper_and_expiry(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "test-secret")
    s = oauth.sign_state({"u": 7, "p": "google", "exp": time.time() + 100})
    assert oauth.verify_state(s)["u"] == 7
    # tamper -> rejected
    assert oauth.verify_state(s[:-1] + ("0" if s[-1] != "0" else "1")) is None
    # expired -> rejected
    old = oauth.sign_state({"u": 7, "p": "google", "exp": time.time() - 1})
    assert oauth.verify_state(old) is None
    # different secret can't verify
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "other")
    assert oauth.verify_state(s) is None


def test_configured_reads_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert oauth.configured("google") is False
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    assert oauth.configured("google") is True
    assert oauth.configured("bogus") is False


def test_authorize_url_has_scopes_state_and_offline(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    url = oauth.authorize_url("google", redirect_uri="https://x/cb", user_id=3)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "gmail.readonly" in url and "calendar.readonly" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "client_id=cid" in url and "state=" in url


# ── token exchange / refresh / storage ────────────────────────────────────────
def test_exchange_and_refresh_call_token_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    seen = {}
    def fake_post(url, data=None, timeout=None):
        seen["url"] = url; seen["data"] = data
        return _Resp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    tok = oauth.exchange_code("google", code="abc", redirect_uri="https://x/cb")
    assert tok["access_token"] == "AT"
    assert seen["url"] == "https://oauth2.googleapis.com/token"
    assert seen["data"]["grant_type"] == "authorization_code"
    oauth.refresh_access_token("google", refresh_token="RT")
    assert seen["data"]["grant_type"] == "refresh_token"


def test_save_tokens_upserts_and_preserves_refresh_token(db):
    r1 = oauth.save_tokens(db, user_id=1, provider="google", account_email="a@x.com",
                           tokens={"access_token": "AT1", "refresh_token": "RT1",
                                   "expires_in": 3600, "scope": "openid email"})
    assert r1.refresh_token == "RT1" and r1.status == "active"
    # a re-auth that omits refresh_token must NOT wipe the stored one
    r2 = oauth.save_tokens(db, user_id=1, provider="google", account_email="a@x.com",
                           tokens={"access_token": "AT2", "expires_in": 3600})
    assert r2.id == r1.id                       # upserted, not duplicated
    assert r2.access_token == "AT2" and r2.refresh_token == "RT1"   # preserved
    assert db.query(models.ConnectedAccount).count() == 1


def test_get_valid_token_refreshes_when_expired(db, monkeypatch):
    row = oauth.save_tokens(db, user_id=1, provider="google", account_email="a@x.com",
                            tokens={"access_token": "OLD", "refresh_token": "RT",
                                    "expires_in": 3600})
    # not expired -> returned as-is, no refresh
    assert oauth.get_valid_access_token(db, row) == "OLD"
    # force expiry -> refresh path
    row.token_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    monkeypatch.setattr(oauth.httpx, "post",
                        lambda *a, **k: _Resp({"access_token": "NEW", "expires_in": 3600}))
    assert oauth.get_valid_access_token(db, row) == "NEW"
    assert row.status == "active"


def test_get_valid_token_no_refresh_token_marks_error(db):
    row = oauth.save_tokens(db, user_id=1, provider="google", account_email="a@x.com",
                            tokens={"access_token": "AT", "expires_in": 3600})
    row.refresh_token = ""                       # nothing to refresh with
    row.token_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    assert oauth.get_valid_access_token(db, row) is None
    assert row.status == "error"
