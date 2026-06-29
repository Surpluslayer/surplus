"""Tests for email+password signup / sign-in.

Hashing (bcrypt round-trip, long-password safety, wrong-password), and the routes via
TestClient: signup creates an account + session cookie, duplicate email -> 409, login
verifies, bad creds -> generic 401, native client -> Bearer token, and the unification
(a password account + a Google login on the same email stay ONE User).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.db as db_mod
from backend.db import Base, get_db
from backend import models
from backend import auth as auth_mod
from backend.main import app


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override
    # Disable rate limiting: the limiter is global module state, so a monkeypatch
    # wouldn't unbind the already-registered Depends -- override the dep itself.
    from backend.routes import password_auth as pa
    app.dependency_overrides[pa._rl_signup] = lambda: None
    app.dependency_overrides[pa._rl_login] = lambda: None
    yield TestClient(app), TestingSession
    app.dependency_overrides.clear()


# ── hashing ───────────────────────────────────────────────────────────────────
def test_hash_round_trip_and_wrong_password():
    h = auth_mod.hash_password("hunter2pw")
    assert h and h != "hunter2pw"
    assert auth_mod.verify_password("hunter2pw", h) is True
    assert auth_mod.verify_password("wrong", h) is False
    assert auth_mod.verify_password("x", None) is False      # OAuth-only user


def test_hash_handles_long_password():
    pw = "a" * 200                                            # > bcrypt's 72-byte limit
    h = auth_mod.hash_password(pw)
    assert auth_mod.verify_password(pw, h) is True
    assert auth_mod.verify_password("a" * 199, h) is False


# ── signup ────────────────────────────────────────────────────────────────────
def test_signup_creates_account_and_cookie(client):
    c, _ = client
    r = c.post("/api/auth/signup",
               json={"name": "Jane", "email": "Jane@X.com", "password": "secret12"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "jane@x.com"             # normalized
    assert "surplus_session" in r.cookies


def test_signup_rejects_short_password(client):
    c, _ = client
    r = c.post("/api/auth/signup",
               json={"name": "J", "email": "j@x.com", "password": "short"})
    assert r.status_code == 400


def test_signup_duplicate_email_409(client):
    c, _ = client
    c.post("/api/auth/signup", json={"name": "J", "email": "j@x.com", "password": "secret12"})
    r = c.post("/api/auth/signup", json={"name": "J2", "email": "j@x.com", "password": "secret12"})
    assert r.status_code == 409


def test_signup_does_not_overwrite_oauth_account(client):
    """An OAuth (passwordless) user owns the email; a signup must NOT attach a password
    to it (takeover) -- it 409s instead."""
    c, Session = client
    s = Session()
    s.add(models.User(email="g@x.com", name="G", google_sub="G1")); s.commit(); s.close()
    r = c.post("/api/auth/signup", json={"name": "X", "email": "g@x.com", "password": "secret12"})
    assert r.status_code == 409


# ── login ─────────────────────────────────────────────────────────────────────
def test_login_success_and_session_works(client):
    c, _ = client
    c.post("/api/auth/signup", json={"name": "J", "email": "j@x.com", "password": "secret12"})
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"email": "j@x.com", "password": "secret12"})
    assert r.status_code == 200 and "surplus_session" in r.cookies


def test_login_wrong_password_generic_401(client):
    c, _ = client
    c.post("/api/auth/signup", json={"name": "J", "email": "j@x.com", "password": "secret12"})
    r = c.post("/api/auth/login", json={"email": "j@x.com", "password": "nope"})
    assert r.status_code == 401 and r.json()["detail"] == "invalid email or password"


def test_login_unknown_email_generic_401(client):
    c, _ = client
    r = c.post("/api/auth/login", json={"email": "nobody@x.com", "password": "whatever1"})
    assert r.status_code == 401


def test_login_oauth_only_user_cannot_password_login(client):
    c, Session = client
    s = Session()
    s.add(models.User(email="g@x.com", name="G", google_sub="G1")); s.commit(); s.close()
    r = c.post("/api/auth/login", json={"email": "g@x.com", "password": "anything1"})
    assert r.status_code == 401                          # no password_hash -> rejected


# ── native client + unification ───────────────────────────────────────────────
def test_native_client_gets_bearer_token(client):
    c, _ = client
    r = c.post("/api/auth/signup",
               json={"name": "J", "email": "p@x.com", "password": "secret12", "client": "plugin"})
    assert r.status_code == 200
    assert r.json().get("token") and r.json().get("client") == "plugin"
    assert "surplus_session" not in r.cookies            # native uses the token, not a cookie


def test_password_then_google_is_one_user(client):
    """A password account + a Google login on the same email = ONE User."""
    c, Session = client
    c.post("/api/auth/signup", json={"name": "J", "email": "dual@x.com", "password": "secret12"})
    s = Session()
    u = auth_mod.find_or_create_oauth_user(
        s, provider="google", sub="G1", email="dual@x.com", name="J")
    assert s.query(models.User).filter(models.User.email == "dual@x.com").count() == 1
    assert u.google_sub == "G1" and u.password_hash is not None
    s.close()


def test_signup_verification_required_reflects_send(client, monkeypatch):
    """The gate is conditional: required ONLY when a code actually sends (email live),
    so a dormant provider never locks a new user out."""
    c, _ = client
    # dormant email (no RESEND in tests) -> send returns False -> NOT required
    r = c.post("/api/auth/signup", json={"name": "A", "email": "a@x.com", "password": "secret12"})
    assert r.json().get("verification_required") is False
    # when a code DID send -> required
    monkeypatch.setattr("backend.routes.account_email.send_verification_code", lambda db, u: True)
    r2 = c.post("/api/auth/signup", json={"name": "B", "email": "b@x.com", "password": "secret12"})
    assert r2.json().get("verification_required") is True
