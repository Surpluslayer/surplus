"""
Tests for the WhatsApp-channel connect flow (a CLOUD Unipile seat, like the
email/LinkedIn seats -- NOT a device companion).

  * POST /api/auth/whatsapp/start mints a hosted link for the SIGNED-IN user.
  * Unipile's notify webhook attaches the new account_id to that user's row,
    flipping whatsapp_status='active' (with cross-user release).
  * /me exposes whatsapp_status + unipile_whatsapp_account_id.

Mirrors test_email_connect.py : direct route-function calls + in-memory SQLite,
avoiding the FastAPI app import (Python 3.9 `str | None` eval issue). Async
handlers run on a fresh event loop per call. All Unipile calls are mocked.
"""
from __future__ import annotations
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import auth as auth_route


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


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Host"),
                    email=kw.get("email", "host@x.com"),
                    unipile_account_id=kw.get("acct", "li_acct_1"))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _auth_state(db, user, token="tok-wa-1"):
    st = auth_route.AuthState(state_token=token, status="pending",
                              user_id=user.id)
    db.add(st); db.commit()
    return st


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── hosted-link body ─────────────────────────────────────────────────────────

def test_whatsapp_create_body_targets_whatsapp_provider_and_routes():
    body = auth_route._whatsapp_create_body(
        "https://api40.unipile.com:17054", "2026-01-01T00:00:00.000Z",
        "tok123", "https://www.surpluslayer.com", "https://x/fail")
    assert body["providers"] == ["WHATSAPP"]
    assert body["type"] == "create"
    assert body["name"] == "tok123"
    assert body["notify_url"].endswith("/api/auth/whatsapp/webhook")
    assert "/api/auth/whatsapp/callback?state=tok123" in body["success_redirect_url"]


# ── webhook : attach to the pre-tagged user ──────────────────────────────────

def test_webhook_attaches_whatsapp_account_to_pretagged_user(db, monkeypatch):
    u = _user(db)
    _auth_state(db, u, token="tok-1")
    # No dsn/key -> the post-commit first-sync thread is not kicked (keeps the
    # test free of any network / Unipile call).
    monkeypatch.setattr(auth_route, "_unipile_dsn", lambda: None)
    monkeypatch.setattr(auth_route, "_unipile_api_key", lambda: None)

    resp = _run(auth_route.whatsapp_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "wa_acct_9",
         "name": "tok-1"}, db))
    assert json.loads(resp.body)["user_id"] == u.id

    db.refresh(u)
    assert u.unipile_whatsapp_account_id == "wa_acct_9"
    assert u.whatsapp_status == "active"
    assert u.whatsapp_connected_at is not None
    # Other seats untouched -- the channels are independent.
    assert u.unipile_account_id == "li_acct_1"
    assert (u.email_status or "disconnected") == "disconnected"

    st = (db.query(auth_route.AuthState)
          .filter_by(state_token="tok-1").first())
    assert st.status == "webhook_done"


def test_webhook_failure_marks_authstate_and_leaves_user(db):
    u = _user(db)
    _auth_state(db, u, token="tok-2")
    resp = _run(auth_route.whatsapp_webhook(
        {"status": "CREATION_FAILED", "account_id": "", "name": "tok-2"}, db))
    assert json.loads(resp.body)["recorded"] == "failure"
    db.refresh(u)
    assert u.unipile_whatsapp_account_id is None
    assert (u.whatsapp_status or "disconnected") == "disconnected"


def test_webhook_unknown_token_is_ignored(db):
    resp = _run(auth_route.whatsapp_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "a", "name": "ghost"}, db))
    assert json.loads(resp.body)["ignored"] == "unknown state_token"


def test_webhook_moves_whatsapp_account_between_users(db, monkeypatch):
    """Re-connecting the same WhatsApp from a different user row must release
    it from the old row first (unique index) -- one account, one owner."""
    old = _user(db, email="old@x.com", acct="li_old")
    old.unipile_whatsapp_account_id = "wa_shared"
    old.whatsapp_status = "active"
    db.commit()
    new = _user(db, email="new@x.com", acct="li_new")
    _auth_state(db, new, token="tok-3")
    monkeypatch.setattr(auth_route, "_unipile_dsn", lambda: None)
    monkeypatch.setattr(auth_route, "_unipile_api_key", lambda: None)

    _run(auth_route.whatsapp_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "wa_shared",
         "name": "tok-3"}, db))
    db.refresh(old); db.refresh(new)
    assert new.unipile_whatsapp_account_id == "wa_shared"
    assert new.whatsapp_status == "active"
    assert old.unipile_whatsapp_account_id is None
    assert old.whatsapp_status == "disconnected"


# ── /me exposure ─────────────────────────────────────────────────────────────

def test_me_exposes_whatsapp_channel_fields(db):
    u = _user(db)
    u.unipile_whatsapp_account_id = "wa_1"
    u.whatsapp_status = "active"
    db.commit(); db.refresh(u)
    payload = json.loads(auth_route.me(u).body)
    assert payload["whatsapp_status"] == "active"
    assert payload["unipile_whatsapp_account_id"] == "wa_1"


def test_me_defaults_disconnected_for_legacy_rows(db):
    u = _user(db)
    payload = json.loads(auth_route.me(u).body)
    assert payload["whatsapp_status"] == "disconnected"
    assert payload["unipile_whatsapp_account_id"] is None


# ── migration idempotence ────────────────────────────────────────────────────

def test_whatsapp_migration_idempotent(monkeypatch):
    from backend import db as dbmod
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)  # fresh schema already has the columns
    monkeypatch.setattr(dbmod, "ENGINE", engine)
    dbmod._migrate_user_whatsapp_account()
    dbmod._migrate_user_whatsapp_account()  # second run: no crash, no dup column
