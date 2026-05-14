"""
End-to-end API test — the full five-stage flow over HTTP.

Drives intake -> pipeline -> matching -> roi through the real FastAPI app and
asserts each stage hands off cleanly to the next.
"""
import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db
from backend.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_full_flow(client):
    # ---- 01 intake -------------------------------------------------------
    r = client.post("/events", json={
        "role": "Infra engineers", "seniority": "Senior", "co_stage": "Seed",
        "headcount": 12, "format": "Hackathon", "city": "SF",
        "goal": "Hiring pipeline", "budget": 9000,
    })
    assert r.status_code == 201
    ev = r.json()
    eid = ev["id"]
    assert ev["funnel_target"] == round(12 / 0.6)

    # ---- 02-03 pipeline --------------------------------------------------
    r = client.post(f"/events/{eid}/run")
    assert r.status_code == 200
    run = r.json()
    assert run["counts"]["surfaced"] > 0
    assert run["event"]["threshold"] >= 55
    # every prospect carries a score, a reason, and a status
    for p in run["prospects"]:
        assert 0 <= p["fit_score"] <= 100
        assert p["fit_reason"]
        assert p["status"] in {"surfaced", "below", "contacted", "rsvp"}

    # ---- 04 matching -----------------------------------------------------
    r = client.post(f"/events/{eid}/match")
    if r.status_code == 409:
        pytest.skip("no RSVPs in this seeded run — outreach funnel produced none")
    assert r.status_code == 200
    match = r.json()
    # symbiotic edges only ever connect different market sides
    members = {m["id"]: m for g in match["groups"] for m in g["members"]}
    for e in match["edges"]:
        if e["edge_type"] == "symbiotic":
            assert members[e["a_id"]]["side"] != members[e["b_id"]]["side"]

    # ---- 05 roi ----------------------------------------------------------
    r = client.get(f"/events/{eid}/roi")
    assert r.status_code == 200
    roi = r.json()
    total_attendees = sum(g["builds"] + g["counterparts"] for g in match["groups"])
    assert len(roi["ledger"]) == total_attendees   # one ledger row per guest
    assert roi["metrics"]["budget"] == 9000
    assert roi["metrics"]["attended"] == total_attendees
    assert "net_roi_pct" in roi["metrics"]


def test_run_requires_real_event(client):
    assert client.post("/events/9999/run").status_code == 404


def test_match_before_run_is_conflict(client):
    eid = client.post("/events", json={"headcount": 12}).json()["id"]
    assert client.post(f"/events/{eid}/match").status_code == 409
