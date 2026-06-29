"""Tests for email verification + password reset.

Tokens are stateless (HMAC-signed); the email sender is DORMANT in tests (no
RESEND_API_KEY) so send_email no-ops -- we assert flow + token behavior, not delivery.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend import models
from backend import auth as auth_mod
from backend.integrations import oauth, email_sender
from backend.routes import account_email as ae
from backend.main import app


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)   # sender dormant


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        s = Session()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[ae._rl_forgot] = lambda: None
    yield TestClient(app), Session
    app.dependency_overrides.clear()


# ── tokens ────────────────────────────────────────────────────────────────────
def test_token_round_trip_and_purpose_isolation():
    t = ae._sign("reset_password", 7, 60)
    assert ae._verify(t, "reset_password") == 7
    assert ae._verify(t, "verify_email") == 0      # wrong purpose -> rejected
    assert ae._verify("garbage", "reset_password") == 0


def test_expired_token_rejected():
    t = oauth.sign_state({"purpose": "reset_password", "uid": 7, "exp": time.time() - 1})
    assert ae._verify(t, "reset_password") == 0


def test_sender_dormant_without_key():
    assert email_sender.configured() is False
    assert email_sender.send_email(to="a@x.com", subject="s", text="t") is False


# ── verify-email ──────────────────────────────────────────────────────────────
def test_verify_email_marks_verified(client):
    c, Session = client
    s = Session(); u = models.User(name="U", email="a@x.com", password_hash="h"); s.add(u); s.commit()
    uid = u.id; s.close()
    token = ae._sign("verify_email", uid, 3600)
    r = c.get(f"/api/auth/verify-email?token={token}", follow_redirects=False)
    assert r.status_code == 302 and "status=ok" in r.headers["location"]
    s = Session(); assert s.get(models.User, uid).email_verified is True; s.close()


def test_verify_email_bad_token_redirects_invalid(client):
    c, _ = client
    r = c.get("/api/auth/verify-email?token=nope", follow_redirects=False)
    assert r.status_code == 302 and "status=invalid" in r.headers["location"]


# ── forgot / reset ────────────────────────────────────────────────────────────
def test_forgot_password_always_200_no_enumeration(client):
    c, Session = client
    s = Session(); s.add(models.User(name="U", email="a@x.com", password_hash="h")); s.commit(); s.close()
    assert c.post("/api/auth/forgot-password", json={"email": "a@x.com"}).status_code == 200
    # unknown email -> same 200 (no leak)
    assert c.post("/api/auth/forgot-password", json={"email": "nobody@x.com"}).status_code == 200


def test_reset_password_sets_new_password(client):
    c, Session = client
    s = Session()
    u = models.User(name="U", email="a@x.com", password_hash=auth_mod.hash_password("oldpass12"))
    s.add(u); s.commit(); uid = u.id; s.close()
    token = ae._sign("reset_password", uid, 1800)
    r = c.post("/api/auth/reset-password", json={"token": token, "password": "brandnew123"})
    assert r.status_code == 200
    s = Session(); u2 = s.get(models.User, uid)
    assert auth_mod.verify_password("brandnew123", u2.password_hash) is True
    assert auth_mod.verify_password("oldpass12", u2.password_hash) is False
    s.close()


def test_reset_password_rejects_bad_token(client):
    c, _ = client
    assert c.post("/api/auth/reset-password",
                  json={"token": "bad", "password": "brandnew123"}).status_code == 400


def test_reset_password_rejects_short_password(client):
    c, Session = client
    s = Session(); u = models.User(name="U", email="a@x.com", password_hash="h"); s.add(u); s.commit()
    uid = u.id; s.close()
    token = ae._sign("reset_password", uid, 1800)
    assert c.post("/api/auth/reset-password",
                  json={"token": token, "password": "short"}).status_code == 400
