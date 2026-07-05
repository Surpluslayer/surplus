"""Tests for backend.audit + the RBAC / access-audit wiring on the admin surface
(Phase 4: access, monitoring, resilience).

Covers:
  - audit.record writes a metadata-only row
  - audit.client_ip header precedence (CF > XFF > socket)
  - audit.ip_allowed allowlist semantics (open by default, fail-closed when set)
  - the two-token RBAC split (full vs read-only) and least privilege
  - every admin access — allowed AND denied — leaves an audit row
  - GET /admin/audit-log read endpoint (reachable read-only, outcome filter)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import audit, models
from backend.db import Base, get_db
from backend.routes import admin as admin_route


@pytest.fixture
def db():
    # StaticPool: one shared in-memory DB across connections/threads, so the
    # TestClient request thread sees the tables create_all made (classic
    # sqlite ':memory:' + TestClient gotcha).
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _req(headers=None, method="POST", path="/admin/delete-user", host="10.0.0.9"):
    """A minimal stand-in for a Starlette Request for the gate/audit code."""
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        headers={k.lower(): v for k, v in (headers or {}).items()},
        client=SimpleNamespace(host=host),
    )


# ── audit.record ─────────────────────────────────────────────────────────

def test_record_writes_metadata_row(db):
    audit.record(db, actor="admin:admin", action="POST /admin/x",
                 target="user:7", outcome="allowed", source_ip="1.2.3.4",
                 detail="")
    rows = db.query(models.AuditLog).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.actor == "admin:admin"
    assert r.action == "POST /admin/x"
    assert r.target == "user:7"
    assert r.outcome == "allowed"
    assert r.source_ip == "1.2.3.4"


def test_record_noop_without_usable_session():
    # Must not raise when db isn't a Session (dependency called outside FastAPI).
    audit.record(None, actor="anon", action="admin")
    audit.record(object(), actor="anon", action="admin")


# ── audit.client_ip ──────────────────────────────────────────────────────

def test_client_ip_prefers_cf_then_xff_then_socket():
    assert audit.client_ip(_req({"CF-Connecting-IP": "9.9.9.9",
                                 "X-Forwarded-For": "1.1.1.1, 2.2.2.2"})) == "9.9.9.9"
    assert audit.client_ip(_req({"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})) == "1.1.1.1"
    assert audit.client_ip(_req(host="7.7.7.7")) == "7.7.7.7"
    assert audit.client_ip(None) == ""


# ── audit.ip_allowed ─────────────────────────────────────────────────────

def test_ip_allowed_open_by_default(monkeypatch):
    monkeypatch.delenv("ADMIN_IP_ALLOWLIST", raising=False)
    assert audit.ip_allowed("203.0.113.5") is True
    assert audit.ip_allowed("") is True


def test_ip_allowed_enforces_and_fails_closed(monkeypatch):
    monkeypatch.setenv("ADMIN_IP_ALLOWLIST", "203.0.113.0/24, 10.0.0.9")
    assert audit.ip_allowed("203.0.113.5") is True
    assert audit.ip_allowed("10.0.0.9") is True
    assert audit.ip_allowed("198.51.100.1") is False
    # allowlist configured but caller IP unknown/garbage -> denied
    assert audit.ip_allowed("") is False
    assert audit.ip_allowed("not-an-ip") is False


# ── RBAC: _admin_role ────────────────────────────────────────────────────

def test_admin_role_resolution(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    monkeypatch.setenv("ADMIN_READONLY_TOKEN", "ro-tok")
    assert admin_route._admin_role("full-tok") == "admin"
    assert admin_route._admin_role("ro-tok") == "readonly"
    assert admin_route._admin_role("nope") is None
    assert admin_route._admin_role(None) is None


# ── _check_admin gate + audit trail ──────────────────────────────────────

def test_write_gate_allows_full_token_and_audits(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    role = admin_route._check_admin(need_write=True, x_admin_token="full-tok",
                                    request=_req(), db=db)
    assert role == "admin"
    row = db.query(models.AuditLog).one()
    assert row.outcome == "allowed"
    assert row.actor == "admin:admin"
    assert row.action == "POST /admin/delete-user"


def test_write_gate_rejects_readonly_token_but_audits(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    monkeypatch.setenv("ADMIN_READONLY_TOKEN", "ro-tok")
    with pytest.raises(HTTPException) as exc:
        admin_route._check_admin(need_write=True, x_admin_token="ro-tok",
                                 request=_req(), db=db)
    assert exc.value.status_code == 404
    row = db.query(models.AuditLog).one()
    assert row.outcome == "denied"
    assert row.detail == "insufficient_role"
    assert row.actor == "admin:readonly"


def test_readonly_gate_accepts_both_tokens(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    monkeypatch.setenv("ADMIN_READONLY_TOKEN", "ro-tok")
    assert admin_route._check_admin(need_write=False, x_admin_token="ro-tok",
                                    request=_req(path="/admin/audit-log",
                                                 method="GET"), db=db) == "readonly"
    assert admin_route._check_admin(need_write=False, x_admin_token="full-tok",
                                    request=_req(path="/admin/audit-log",
                                                 method="GET"), db=db) == "admin"


def test_bad_token_denied_and_audited(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    with pytest.raises(HTTPException) as exc:
        admin_route._check_admin(need_write=True, x_admin_token="wrong",
                                 request=_req(), db=db)
    assert exc.value.status_code == 404
    row = db.query(models.AuditLog).one()
    assert row.outcome == "denied"
    assert row.detail == "bad_token"
    assert row.actor == "anon"


def test_no_admin_configured_is_404_without_audit(db, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_READONLY_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        admin_route._check_admin(need_write=True, x_admin_token="anything",
                                 request=_req(), db=db)
    assert exc.value.status_code == 404
    assert db.query(models.AuditLog).count() == 0


def test_ip_allowlist_blocks_and_audits(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    monkeypatch.setenv("ADMIN_IP_ALLOWLIST", "203.0.113.0/24")
    with pytest.raises(HTTPException) as exc:
        admin_route._check_admin(need_write=True, x_admin_token="full-tok",
                                 request=_req(host="198.51.100.7"), db=db)
    assert exc.value.status_code == 404
    row = db.query(models.AuditLog).one()
    assert row.outcome == "denied"
    assert row.detail == "ip_not_allowlisted"


# ── GET /admin/audit-log endpoint ────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "full-tok")
    monkeypatch.setenv("ADMIN_READONLY_TOKEN", "ro-tok")
    app = FastAPI()
    app.include_router(admin_route.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_audit_log_endpoint_readonly_and_filter(client, db):
    # A denied write attempt (readonly token on a mutating route) leaves a row.
    r = client.post("/admin/delete-user", headers={"X-Admin-Token": "ro-tok"},
                    json={"user_id": 1})
    assert r.status_code == 404

    # Read the trail with the least-privilege read-only token.
    r = client.get("/admin/audit-log", headers={"X-Admin-Token": "ro-tok"})
    assert r.status_code == 200
    body = r.json()
    assert any(row["outcome"] == "denied" for row in body)

    # outcome filter narrows to denied rows only.
    r = client.get("/admin/audit-log?outcome=denied",
                   headers={"X-Admin-Token": "ro-tok"})
    assert r.status_code == 200
    assert all(row["outcome"] == "denied" for row in r.json())

    # No token at all -> 404 (endpoint stays invisible).
    assert client.get("/admin/audit-log").status_code == 404
