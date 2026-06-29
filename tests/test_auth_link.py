"""Tests for the safe-migration auth: set-password + link_oauth_identity.

set-password lets a signed-in OAuth/LinkedIn user add email+password without a duplicate.
link_oauth_identity attaches a provider sub to an EXISTING user (the "link while logged
in" path), refusing if the sub already belongs to someone else.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend import models
from backend import auth as auth_mod
from backend.main import app


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ── link_oauth_identity ───────────────────────────────────────────────────────
def test_link_attaches_sub_to_existing_user(db):
    s = db()
    u = models.User(name="LI", email="a@x.com", unipile_account_id="li1")
    s.add(u); s.commit()
    assert auth_mod.link_oauth_identity(s, u, provider="google", sub="G1") is True
    assert u.google_sub == "G1" and u.unipile_account_id == "li1"  # kept LinkedIn too


def test_link_refuses_sub_owned_by_another_user(db):
    s = db()
    other = models.User(name="Other", google_sub="G1")
    me = models.User(name="Me", unipile_account_id="li1")
    s.add_all([other, me]); s.commit()
    assert auth_mod.link_oauth_identity(s, me, provider="google", sub="G1") is False
    assert me.google_sub is None                       # untouched


def test_link_idempotent_same_user(db):
    s = db()
    u = models.User(name="U", microsoft_sub="M1")
    s.add(u); s.commit()
    assert auth_mod.link_oauth_identity(s, u, provider="microsoft", sub="M1") is True


# ── set-password endpoint (authenticated) ─────────────────────────────────────
def test_set_password_on_signed_in_account(db):
    Session = db
    s = Session()
    u = models.User(name="G", email="g@x.com", google_sub="G1")  # OAuth user, no pw
    s.add(u); s.commit()
    sess = auth_mod.create_session(s, u)
    token = sess.session_token
    s.close()

    def _override():
        d = Session()
        try: yield d
        finally: d.close()
    app.dependency_overrides[get_db] = _override
    try:
        c = TestClient(app)
        # unauthenticated -> 401
        assert c.post("/api/auth/set-password", json={"password": "newsecret1"}).status_code == 401
        # with the session cookie -> sets the password
        r = c.post("/api/auth/set-password", json={"password": "newsecret1"},
                   cookies={"surplus_session": token})
        assert r.status_code == 200, r.text
        # too short -> 400
        assert c.post("/api/auth/set-password", json={"password": "x"},
                      cookies={"surplus_session": token}).status_code == 400
    finally:
        app.dependency_overrides.clear()

    s = Session()
    u2 = s.query(models.User).filter_by(email="g@x.com").one()
    assert auth_mod.verify_password("newsecret1", u2.password_hash) is True
    s.close()
