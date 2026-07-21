"""The staging gate (main.staging_gate): non-prod deployments must not be
publicly browsable. Token comes from SURPLUS_STAGING_GATE; unset = pass-through
so prod and local dev are untouched."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_gate_off_is_passthrough(client, monkeypatch):
    monkeypatch.delenv("SURPLUS_STAGING_GATE", raising=False)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code != 404


def test_gate_on_404s_without_token(client, monkeypatch):
    monkeypatch.setenv("SURPLUS_STAGING_GATE", "sekrit")
    assert client.get("/").status_code == 404
    assert client.get("/api/auth/me").status_code == 404
    assert client.get("/?staging_key=wrong").status_code == 404


def test_gate_keeps_health_and_webhooks_open(client, monkeypatch):
    monkeypatch.setenv("SURPLUS_STAGING_GATE", "sekrit")
    assert client.get("/api/health").status_code == 200
    # Webhooks pass the gate and hit their own fail-closed verifiers instead
    # (401/503/400 from the handler, never the gate's 404 text).
    r = client.post("/webhooks/unipile", json={})
    assert r.status_code != 404


def test_gate_key_sets_cookie_and_admits(client, monkeypatch):
    monkeypatch.setenv("SURPLUS_STAGING_GATE", "sekrit")
    r = client.get("/?staging_key=sekrit", follow_redirects=False)
    assert r.status_code == 303
    assert "surplus_staging_gate" in r.headers.get("set-cookie", "")
    r2 = client.get("/api/auth/me", cookies={"surplus_staging_gate": "sekrit"})
    assert r2.status_code != 404
    # Wrong cookie stays out.
    r3 = client.get("/api/auth/me", cookies={"surplus_staging_gate": "nope"})
    assert r3.status_code == 404
