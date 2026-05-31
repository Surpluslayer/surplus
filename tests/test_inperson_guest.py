"""
In-person guest access (event.surpluslayer.com only).

A tester can use the capture flow without LinkedIn : POST /api/auth/inperson/guest
mints a LinkedIn-less session so create-event / resolve / scan / captures work,
while real LinkedIn SENDS stay blocked (no connected account). The guest door is
gated to the in-person host so the apex product keeps its sign-in gate.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db
from backend.main import app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("INPERSON_HOSTS", "event.surpluslayer.com")
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    from backend import rate_limit
    rate_limit._WINDOWS.clear()   # guest-mint quota must not leak across tests
    reset_db()
    yield


def _event_client():
    return TestClient(app, base_url="https://event.surpluslayer.com")


def test_guest_rejected_on_apex_host():
    apex = TestClient(app, base_url="https://www.surpluslayer.com")
    assert apex.post("/api/auth/inperson/guest").status_code == 403


def test_guest_minted_on_event_host_is_linkedinless():
    c = _event_client()
    assert c.get("/api/auth/me").status_code == 401     # no session yet
    g = c.post("/api/auth/inperson/guest")
    assert g.status_code == 200 and g.json()["mode"] == "inperson_guest"
    me = c.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["unipile_account_id"] is None      # not LinkedIn-connected


def test_guest_can_run_capture_flow():
    c = _event_client()
    c.post("/api/auth/inperson/guest")
    eid = c.post("/api/inperson/events", json={"label": "TW", "city": "NYC"}).json()["event_id"]
    r = c.post("/api/inperson/resolve",
               json={"method": "url", "linkedin_url": "https://www.linkedin.com/in/maya-rodriguez"})
    assert r.status_code == 200
    url = r.json()["candidate"]["linkedin_url"]
    sc = c.post("/api/inperson/scan",
                json={"event_id": eid, "linkedin_url": url, "source": "scan", "name": "Maya"})
    assert sc.status_code == 200
    assert sc.json()["prospect"]["status"] == "pending"
    assert sc.json()["draft_message"]
    caps = c.get(f"/api/inperson/events/{eid}/captures")
    assert caps.status_code == 200 and caps.json()["count"] == 1


def test_guest_send_is_blocked():
    c = _event_client()
    c.post("/api/auth/inperson/guest")
    eid = c.post("/api/inperson/events", json={"label": "TW", "city": "NYC"}).json()["event_id"]
    url = c.post("/api/inperson/resolve",
                 json={"method": "url",
                       "linkedin_url": "https://www.linkedin.com/in/maya-rodriguez"}
                 ).json()["candidate"]["linkedin_url"]
    pid = c.post("/api/inperson/scan",
                 json={"event_id": eid, "linkedin_url": url, "source": "scan", "name": "Maya"}
                 ).json()["prospect"]["prospect_id"]
    r = c.post(f"/api/inperson/captures/{pid}/send", json={})
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "linkedin_send_locked"
