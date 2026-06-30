"""Tests for the plugin LinkedIn cookie-connect (integrations/linkedin_cookie + routes).

No live Unipile: httpx is mocked. Covers connect_with_cookie (config/cookie guards,
success shape, error mapping) and the route's dedup guard -- the crucial bit: an
already-connected user is a no-op that NEVER calls Unipile (no duplicate account).
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
from backend.integrations import linkedin_cookie
from backend.main import app


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.content = data, status, b"x"
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)
    def json(self):
        return self._data


class _Client:
    def __init__(self, data): self._data = data
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _Resp(self._data)


# ── connect_with_cookie ───────────────────────────────────────────────────────
def test_connect_requires_config(monkeypatch):
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        linkedin_cookie.connect_with_cookie(li_at="x")


def test_connect_requires_cookie(monkeypatch):
    monkeypatch.setenv("UNIPILE_DSN", "api.unipile.com:443")
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    with pytest.raises(ValueError, match="missing LinkedIn cookie"):
        linkedin_cookie.connect_with_cookie(li_at="")


def test_connect_success_returns_account_id(monkeypatch):
    monkeypatch.setenv("UNIPILE_DSN", "api.unipile.com:443")
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    monkeypatch.setattr(linkedin_cookie.httpx, "Client",
                        lambda *a, **k: _Client({"account_id": "ACC1"}))
    out = linkedin_cookie.connect_with_cookie(li_at="li_at_value", user_agent="UA")
    assert out["account_id"] == "ACC1"


def test_connect_missing_account_id_raises(monkeypatch):
    monkeypatch.setenv("UNIPILE_DSN", "api.unipile.com:443")
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    monkeypatch.setattr(linkedin_cookie.httpx, "Client",
                        lambda *a, **k: _Client({"unexpected": True}))
    with pytest.raises(ValueError, match="did not return an account id"):
        linkedin_cookie.connect_with_cookie(li_at="x")


# ── routes (dedup guard) ──────────────────────────────────────────────────────
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
    yield TestClient(app), Session
    app.dependency_overrides.clear()


def _user_with_session(Session, **kw):
    s = Session()
    u = models.User(name="U", **kw)
    s.add(u); s.commit()
    tok = auth_mod.create_session(s, u).session_token
    s.close()
    return tok


def test_status_reports_connection(client):
    c, Session = client
    tok = _user_with_session(Session, unipile_account_id="ACC1", linkedin_status="active")
    c.cookies.set("surplus_session", tok)
    r = c.get("/api/integrations/linkedin/status")
    assert r.status_code == 200 and r.json()["connected"] is True


def test_connect_cookie_noop_when_already_connected(client, monkeypatch):
    """The duplicate-prevention guarantee: already-active -> reuse, Unipile NOT called."""
    c, Session = client
    tok = _user_with_session(Session, unipile_account_id="ACC1", linkedin_status="active")
    c.cookies.set("surplus_session", tok)
    def _boom(*a, **k):
        raise AssertionError("Unipile must NOT be called when already connected")
    monkeypatch.setattr(linkedin_cookie, "connect_with_cookie", _boom)
    r = c.post("/api/integrations/linkedin/connect-cookie", json={"li_at": "x"})
    assert r.status_code == 200 and r.json() == {"connected": True, "account_id": "ACC1", "reused": True}


def test_connect_cookie_connects_when_not_connected(client, monkeypatch):
    c, Session = client
    tok = _user_with_session(Session)            # no unipile account yet
    c.cookies.set("surplus_session", tok)
    monkeypatch.setattr(
        "backend.integrations.linkedin_cookie.connect_with_cookie",
        lambda **k: {"account_id": "NEW1"})
    r = c.post("/api/integrations/linkedin/connect-cookie",
               json={"li_at": "li_at_value", "user_agent": "UA"})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "NEW1" and body["reused"] is False
    s = Session()
    u = s.query(models.User).filter_by(unipile_account_id="NEW1").one()
    assert u.linkedin_status == "active"
    s.close()


# ── orphan-dedup on reconnect / cross-user reassign ───────────────────────────
def test_reconnect_different_account_releases_and_deletes_orphan(client, monkeypatch):
    """Reconnecting (not active, so the no-op guard doesn't fire) with a NEW
    account_id must release the old seat and best-effort delete it from Unipile."""
    c, Session = client
    # User holds a stale account but is NOT active -> connect proceeds.
    tok = _user_with_session(Session, unipile_account_id="OLD1",
                             linkedin_status="disconnected")
    c.cookies.set("surplus_session", tok)
    monkeypatch.setattr(
        "backend.integrations.linkedin_cookie.connect_with_cookie",
        lambda **k: {"account_id": "NEW2"})
    deleted = []
    monkeypatch.setattr(
        "backend.integrations.linkedin_cookie.delete_account",
        lambda acct: deleted.append(acct) or True)
    r = c.post("/api/integrations/linkedin/connect-cookie",
               json={"li_at": "li_at_value"})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "NEW2" and body["reused"] is False
    assert deleted == ["OLD1"]                      # orphan deleted after commit
    s = Session()
    u = s.query(models.User).filter_by(unipile_account_id="NEW2").one()
    assert u.linkedin_status == "active"
    s.close()


def test_connect_account_already_on_another_user_reassigns(client, monkeypatch):
    """If the new account_id is already bound to a DIFFERENT user, release it
    from them (unipile_account_id=None, status disconnected) and bind to us."""
    c, Session = client
    # Pre-existing other user owning SHARED1.
    s = Session()
    other = models.User(name="Other", unipile_account_id="SHARED1",
                        linkedin_status="active")
    s.add(other); s.commit()
    other_id = other.id
    s.close()
    tok = _user_with_session(Session)              # connecting user, no account yet
    c.cookies.set("surplus_session", tok)
    monkeypatch.setattr(
        "backend.integrations.linkedin_cookie.connect_with_cookie",
        lambda **k: {"account_id": "SHARED1"})
    monkeypatch.setattr(
        "backend.integrations.linkedin_cookie.delete_account",
        lambda acct: (_ for _ in ()).throw(
            AssertionError("no orphan delete when caller had no prior account")))
    r = c.post("/api/integrations/linkedin/connect-cookie",
               json={"li_at": "li_at_value"})
    assert r.status_code == 200 and r.json()["account_id"] == "SHARED1"
    s = Session()
    released = s.query(models.User).filter_by(id=other_id).one()
    assert released.unipile_account_id is None
    assert released.linkedin_status == "disconnected"
    # Exactly one user now owns SHARED1.
    owners = s.query(models.User).filter_by(unipile_account_id="SHARED1").all()
    assert len(owners) == 1 and owners[0].id != other_id
    s.close()
