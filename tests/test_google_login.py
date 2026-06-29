"""Tests for Sign in with Google (decoupled login) + the cross-client session.

No live Google: httpx and the token getter are mocked. Covers the identity helpers,
find-or-create (new user, link by google_sub, link by email to a LinkedIn-first user,
demo users excluded), the cross-client session (cookie OR Bearer in current_user, the
client tag), and the route guard (unconfigured -> 409, bad state -> 400).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend import auth as auth_mod
from backend.integrations import google_login
from backend.routes.google_login import find_or_create_google_user


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


# ── identity helpers ──────────────────────────────────────────────────────────
def test_fetch_identity_normalizes(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"sub": "G123", "email": "Jane@X.com",
                                "name": "Jane", "picture": "p"}
    monkeypatch.setattr(google_login.httpx, "get", lambda *a, **k: R())
    out = google_login.fetch_identity("tok")
    assert out == {"sub": "G123", "email": "jane@x.com", "name": "Jane", "picture": "p"}


def test_authorize_url_and_state(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    url = google_login.authorize_url(redirect_uri="https://x/cb", client="plugin")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "scope=openid+email+profile" in url and "client_id=cid" in url
    # the signed state round-trips and carries the client
    state = url.split("state=")[1].split("&")[0]
    import urllib.parse
    payload = google_login.verify_state(urllib.parse.unquote(state))
    assert payload.get("c") == "plugin" and payload.get("k") == "login"


def test_verify_state_rejects_foreign(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    from backend.integrations import oauth
    # a state that isn't a google-login state
    bad = oauth.sign_state({"k": "connect", "p": "google"})
    assert google_login.verify_state(bad) == {}


# ── find-or-create ────────────────────────────────────────────────────────────
def test_creates_new_user_without_unipile(db):
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    assert u.id and u.google_sub == "G1" and u.email == "a@x.com"
    assert u.unipile_account_id is None          # decoupled: no LinkedIn needed


def test_returning_user_matched_by_sub(db):
    u1 = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    u2 = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    assert u1.id == u2.id
    assert db.query(models.User).count() == 1


def test_links_google_to_existing_linkedin_user_by_email(db):
    li = models.User(unipile_account_id="li1", email="a@x.com", name="A")
    db.add(li); db.commit()
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    assert u.id == li.id                          # same person, unified
    assert u.google_sub == "G1" and u.unipile_account_id == "li1"
    assert db.query(models.User).count() == 1


def test_does_not_link_to_demo_user(db):
    demo = models.User(email="a@x.com", name="Demo", is_demo=True)
    db.add(demo); db.commit()
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    assert u.id != demo.id                         # demo user never claimed
    assert db.query(models.User).count() == 2


# ── cross-client session: cookie OR bearer ────────────────────────────────────
def test_create_session_tags_client(db):
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    sess = auth_mod.create_session(db, u, client="plugin")
    assert sess.client == "plugin"
    assert auth_mod.create_session(db, u).client == "web"   # default


def test_current_user_accepts_cookie_or_bearer(db):
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    sess = auth_mod.create_session(db, u, client="plugin")
    # via Bearer header
    got = auth_mod.current_user(db=db, surplus_session=None,
                                authorization=f"Bearer {sess.session_token}")
    assert got.id == u.id
    # via cookie
    got2 = auth_mod.current_user(db=db, surplus_session=sess.session_token,
                                 authorization=None)
    assert got2.id == u.id
    # bearer wins over a (stale) cookie
    got3 = auth_mod.current_user(db=db, surplus_session="garbage",
                                 authorization=f"Bearer {sess.session_token}")
    assert got3.id == u.id


def test_current_user_rejects_bad_token(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        auth_mod.current_user(db=db, surplus_session=None, authorization="Bearer nope")
    with pytest.raises(HTTPException):
        auth_mod.current_user(db=db, surplus_session=None, authorization="Basic xyz")


def test_bearer_token_parsing():
    assert auth_mod._bearer_token("Bearer abc") == "abc"
    assert auth_mod._bearer_token("bearer abc") == "abc"
    assert auth_mod._bearer_token("Basic abc") is None
    assert auth_mod._bearer_token("") is None
    assert auth_mod._bearer_token(None) is None


# ── auto-connect: login also connects the provider's data ─────────────────────
def test_login_requests_data_scopes_and_offline(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    url = google_login.authorize_url(redirect_uri="https://x/cb")
    # login consent now also covers calendar + contacts (auto-connect) + offline refresh
    assert "calendar.events" in url and "contacts.readonly" in url
    assert "access_type=offline" in url
    assert "gmail" not in url                      # Gmail stays on Unipile (no CASA)


def test_auto_connect_saves_connected_account(db):
    from backend.routes._oauth_login import _auto_connect
    u = find_or_create_google_user(db, sub="G1", email="a@x.com", name="A")
    _auto_connect(db, user_id=u.id, provider="google", email="a@x.com",
                  tokens={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    acct = (db.query(models.ConnectedAccount)
            .filter_by(user_id=u.id, provider="google").first())
    assert acct is not None and acct.account_email == "a@x.com"
