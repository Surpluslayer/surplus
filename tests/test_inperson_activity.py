"""
Operator activity roll-up for the in-person host.

GET /api/inperson/activity returns ALL in_person captures across every event
(guests included), operator-only, in-person-host-only. Powers the Activity tab
on event.surpluslayer.com.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db, init_db, SessionLocal
from backend.main import app
from backend import models, auth as authmod


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("INPERSON_HOSTS", "event.surpluslayer.com")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "operator_acct")
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    from backend import rate_limit
    rate_limit._WINDOWS.clear()   # guest-mint quota must not leak across tests
    # /scan's slow half runs detached in prod; run it inline (own session,
    # same DB) so these TestClient flows stay synchronous + deterministic.
    from backend import jobs
    from backend.routes import inperson

    def _inline(fn, *args, prefer_modal=False, **kwargs):
        jobs.execute_detached(jobs._fn_path(fn), *args, **kwargs)
        return "local"
    monkeypatch.setattr(inperson, "run_detached", _inline)
    reset_db(); init_db()  # init_db creates the env-var operator User
    yield


EV = "https://event.surpluslayer.com"


def _guest_client():
    c = TestClient(app, base_url=EV)
    c.post("/api/auth/inperson/guest")
    return c


def _operator_client():
    db = SessionLocal()
    op = db.query(models.User).filter_by(unipile_account_id="operator_acct").first()
    tok = authmod.create_session(db, op).session_token
    db.commit(); db.close()
    c = TestClient(app, base_url=EV)
    c.cookies.set("surplus_session", tok)
    return c


def _capture(c, label, name, url="https://www.linkedin.com/in/maya-rodriguez"):
    eid = c.post("/api/inperson/events", json={"label": label, "city": "NYC"}).json()["event_id"]
    u = c.post("/api/inperson/resolve",
               json={"method": "url", "linkedin_url": url}).json()["candidate"]["linkedin_url"]
    c.post("/api/inperson/scan",
           json={"event_id": eid, "linkedin_url": u, "source": "scan", "name": name})
    return eid


def test_guest_cannot_view_activity():
    g = _guest_client()
    _capture(g, "Guest Mixer", "Maya")
    assert g.get("/api/inperson/activity").status_code == 403


def test_operator_sees_all_events_including_guests():
    g = _guest_client()
    _capture(g, "Guest Mixer", "Maya (guest)")
    op = _operator_client()
    _capture(op, "Operator Dinner", "Dan", url="https://www.linkedin.com/in/dan-wong")

    r = op.get("/api/inperson/activity")
    assert r.status_code == 200
    j = r.json()
    assert j["event_count"] == 2 and j["capture_count"] == 2
    labels = {e["label"]: e for e in j["events"]}
    assert labels["Guest Mixer"]["owner"]["is_guest"] is True
    assert labels["Operator Dinner"]["owner"]["is_guest"] is False
    assert labels["Guest Mixer"]["captures"][0]["name"] == "Maya (guest)"


def test_activity_404_off_inperson_host():
    db = SessionLocal()
    op = db.query(models.User).filter_by(unipile_account_id="operator_acct").first()
    tok = authmod.create_session(db, op).session_token
    db.commit(); db.close()
    apex = TestClient(app, base_url="https://www.surpluslayer.com")
    apex.cookies.set("surplus_session", tok)
    assert apex.get("/api/inperson/activity").status_code == 404
