"""
Tests for the email-channel connect flow (Gmail/Outlook via Unipile hosted
auth) : POST /api/auth/email/start mints a hosted link for the SIGNED-IN
user, Unipile's notify webhook attaches the new account_id to that user's
row, and /me exposes the email_status the Integrations tile reads.

Key property vs the LinkedIn flow: email connect is an INTEGRATION, not a
sign-in — there is no user creation and no identity dedup. The AuthState is
always pre-tagged with user_id, and the webhook only ever writes to that row.

Pattern mirrors test_triage_signup.py : direct route-function calls +
in-memory SQLite, avoiding the FastAPI app import (Python 3.9 / `str | None`
evaluation issue). Async handlers run via asyncio.get_event_loop().
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


def _auth_state(db, user, token="tok-email-1"):
    st = auth_route.AuthState(state_token=token, status="pending",
                              user_id=user.id)
    db.add(st); db.commit()
    return st


def _run(coro):
    # A fresh loop per call : earlier test files may have closed the default
    # loop, which makes get_event_loop() raise mid-suite (passes standalone,
    # fails in the full run — the classic asyncio test trap).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── hosted-link body ─────────────────────────────────────────────────────────

def test_email_create_body_targets_mail_providers_and_email_routes():
    body = auth_route._email_create_body(
        "https://api40.unipile.com:17054", "2026-01-01T00:00:00.000Z",
        "tok123", "https://www.surpluslayer.com", "https://x/fail")
    # "OUTLOOK" (not "MICROSOFT") is Unipile's Microsoft-mail token —
    # the API rejects MICROSOFT with invalid_parameters. Pin it.
    assert body["providers"] == ["GOOGLE", "OUTLOOK"]
    assert body["type"] == "create"
    assert body["name"] == "tok123"
    assert body["notify_url"].endswith("/api/auth/email/webhook")
    assert "/api/auth/email/callback?state=tok123" in body["success_redirect_url"]


# ── mailbox-address extraction ───────────────────────────────────────────────

def test_extract_mailbox_address_known_shapes():
    f = auth_route._extract_mailbox_address
    assert f({"connection_params": {"mail": {"username": "Me@Gmail.com"}}}) \
        == "me@gmail.com"
    assert f({"connection_params": {"mail": {"email": "a@b.co"}}}) == "a@b.co"
    assert f({"identifier": "x@y.io"}) == "x@y.io"
    assert f({"connection_params": {"mail": {"username": "not-an-email"}}}) is None
    assert f({}) is None
    assert f(None) is None


# ── webhook : attach to the pre-tagged user ──────────────────────────────────

def test_webhook_attaches_email_account_to_pretagged_user(db, monkeypatch):
    u = _user(db)
    _auth_state(db, u, token="tok-1")
    monkeypatch.setattr(auth_route, "_unipile_dsn", lambda: "https://dsn")
    monkeypatch.setattr(auth_route, "_unipile_api_key", lambda: "k")

    async def fake_profile(account_id, dsn, api_key):
        return {"connection_params": {"mail": {"username": "host@gmail.com"}}}
    monkeypatch.setattr(auth_route, "_fetch_unipile_profile", fake_profile)

    resp = _run(auth_route.email_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "mail_acct_9",
         "name": "tok-1"}, db))
    assert json.loads(resp.body)["user_id"] == u.id

    db.refresh(u)
    assert u.unipile_email_account_id == "mail_acct_9"
    assert u.email_status == "active"
    assert u.email_account_address == "host@gmail.com"
    assert u.email_connected_at is not None
    # LinkedIn seat untouched — the channels are independent.
    assert u.unipile_account_id == "li_acct_1"

    st = (db.query(auth_route.AuthState)
          .filter_by(state_token="tok-1").first())
    assert st.status == "webhook_done"


def test_webhook_failure_marks_authstate_and_leaves_user(db):
    u = _user(db)
    _auth_state(db, u, token="tok-2")
    resp = _run(auth_route.email_webhook(
        {"status": "CREATION_FAILED", "account_id": "", "name": "tok-2"}, db))
    assert json.loads(resp.body)["recorded"] == "failure"
    db.refresh(u)
    assert u.unipile_email_account_id is None
    assert (u.email_status or "disconnected") == "disconnected"


def test_webhook_unknown_token_is_ignored(db):
    resp = _run(auth_route.email_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "a", "name": "ghost"}, db))
    assert json.loads(resp.body)["ignored"] == "unknown state_token"


def test_webhook_moves_account_between_users(db, monkeypatch):
    """Re-connecting the same mailbox from a different user row must release
    it from the old row first (unique index) — one mailbox, one owner."""
    old = _user(db, email="old@x.com", acct="li_old")
    old.unipile_email_account_id = "mail_shared"
    old.email_status = "active"
    db.commit()
    new = _user(db, email="new@x.com", acct="li_new")
    _auth_state(db, new, token="tok-3")
    monkeypatch.setattr(auth_route, "_unipile_dsn", lambda: None)
    monkeypatch.setattr(auth_route, "_unipile_api_key", lambda: None)

    _run(auth_route.email_webhook(
        {"status": "CREATION_SUCCESS", "account_id": "mail_shared",
         "name": "tok-3"}, db))
    db.refresh(old); db.refresh(new)
    assert new.unipile_email_account_id == "mail_shared"
    assert new.email_status == "active"
    assert old.unipile_email_account_id is None
    assert old.email_status == "disconnected"


# ── /me exposure ─────────────────────────────────────────────────────────────

def test_me_exposes_email_channel_fields(db):
    u = _user(db)
    u.unipile_email_account_id = "mail_1"
    u.email_status = "active"
    u.email_account_address = "host@gmail.com"
    db.commit(); db.refresh(u)
    payload = json.loads(auth_route.me(u).body)
    assert payload["email_status"] == "active"
    assert payload["email_account_address"] == "host@gmail.com"
    assert payload["unipile_email_account_id"] == "mail_1"


def test_me_defaults_disconnected_for_legacy_rows(db):
    u = _user(db)
    payload = json.loads(auth_route.me(u).body)
    assert payload["email_status"] == "disconnected"
    assert payload["email_account_address"] is None


# ── migration idempotence ────────────────────────────────────────────────────

def test_email_migration_idempotent(monkeypatch):
    """Running the column migration twice on the same DB must be a no-op the
    second time (startup runs it on every boot)."""
    from backend import db as dbmod
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)  # fresh schema already has the columns
    monkeypatch.setattr(dbmod, "ENGINE", engine)
    dbmod._migrate_user_email_account()
    dbmod._migrate_user_email_account()  # second run: no crash, no dup column
