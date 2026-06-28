"""Tests for Sign in with Microsoft (Outlook / 365 login) + cross-provider unification.

No live Microsoft: httpx + the token getter are mocked. Covers identity normalization
(mail vs userPrincipalName fallback), the signed state, and the shared find_or_create
keying on microsoft_sub -- plus the key cross-provider case: the SAME person signing in
with Google then Microsoft on one email resolves to ONE User.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend import auth as auth_mod
from backend.integrations import microsoft_login


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


# ── identity helper ───────────────────────────────────────────────────────────
def test_fetch_identity_prefers_mail_then_upn(monkeypatch):
    class R:
        def __init__(self, d): self._d = d
        def raise_for_status(self): pass
        def json(self): return self._d
    # mail present
    monkeypatch.setattr(microsoft_login.httpx, "get",
                        lambda *a, **k: R({"id": "M1", "mail": "Jane@Corp.com",
                                           "displayName": "Jane"}))
    out = microsoft_login.fetch_identity("tok")
    assert out == {"sub": "M1", "email": "jane@corp.com", "name": "Jane", "picture": ""}
    # mail null -> userPrincipalName fallback (common for personal accounts)
    monkeypatch.setattr(microsoft_login.httpx, "get",
                        lambda *a, **k: R({"id": "M2", "mail": None,
                                           "userPrincipalName": "jo@Outlook.com",
                                           "displayName": "Jo"}))
    assert microsoft_login.fetch_identity("tok")["email"] == "jo@outlook.com"


def test_authorize_url_common_endpoint_and_state(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "cid")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    url = microsoft_login.authorize_url(redirect_uri="https://x/cb", client="ios")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "client_id=cid" in url
    import urllib.parse
    state = urllib.parse.unquote(url.split("state=")[1].split("&")[0])
    payload = microsoft_login.verify_state(state)
    assert payload.get("p") == "microsoft" and payload.get("c") == "ios"


def test_verify_state_rejects_google_state(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    from backend.integrations import oauth
    import time
    g = oauth.sign_state({"k": "login", "p": "google", "exp": time.time() + 600})
    assert microsoft_login.verify_state(g) == {}   # provider mismatch -> rejected


# ── find-or-create (shared helper, microsoft) ─────────────────────────────────
def test_creates_microsoft_user_without_unipile(db):
    u = auth_mod.find_or_create_oauth_user(
        db, provider="microsoft", sub="M1", email="a@corp.com", name="A")
    assert u.microsoft_sub == "M1" and u.google_sub is None
    assert u.unipile_account_id is None


def test_returning_microsoft_user_by_sub(db):
    u1 = auth_mod.find_or_create_oauth_user(
        db, provider="microsoft", sub="M1", email="a@corp.com", name="A")
    u2 = auth_mod.find_or_create_oauth_user(
        db, provider="microsoft", sub="M1", email="a@corp.com", name="A")
    assert u1.id == u2.id and db.query(models.User).count() == 1


def test_same_person_google_then_microsoft_is_one_user(db):
    """The unification that matters: one email, two providers -> ONE User."""
    g = auth_mod.find_or_create_oauth_user(
        db, provider="google", sub="G1", email="dual@corp.com", name="Dual")
    m = auth_mod.find_or_create_oauth_user(
        db, provider="microsoft", sub="M1", email="dual@corp.com", name="Dual")
    assert g.id == m.id
    assert m.google_sub == "G1" and m.microsoft_sub == "M1"
    assert db.query(models.User).count() == 1


def test_microsoft_does_not_claim_demo_user(db):
    demo = models.User(email="a@corp.com", name="Demo", is_demo=True)
    db.add(demo); db.commit()
    u = auth_mod.find_or_create_oauth_user(
        db, provider="microsoft", sub="M1", email="a@corp.com", name="A")
    assert u.id != demo.id and db.query(models.User).count() == 2
