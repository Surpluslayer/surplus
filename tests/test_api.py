"""
End-to-end API test — the full five-stage flow over HTTP, including the
Unipile-backed outreach layer and the /webhooks/unipile auto-DM trigger.
"""
import hmac
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db, SessionLocal
from backend.main import app
from backend import models
from backend.providers import reset_provider_cache


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Default: PROVIDER=unipile + DRY_RUN. No real network ever touched."""
    monkeypatch.setenv("PROVIDER", "unipile")
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("UNIPILE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("UNIPILE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    reset_provider_cache()
    reset_db()
    yield
    reset_provider_cache()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ===== existing five-stage flow ============================================

def test_full_flow(client):
    r = client.post("/events", json={
        "role": "Infra engineers", "seniority": "Senior", "co_stage": "Seed",
        "headcount": 12, "format": "Hackathon", "city": "SF",
        "goal": "Hiring pipeline", "budget": 9000,
    })
    assert r.status_code == 201
    ev = r.json()
    eid = ev["id"]
    assert ev["funnel_target"] == round(12 / 0.6)

    r = client.post(f"/events/{eid}/run")
    assert r.status_code == 200
    run = r.json()
    assert run["counts"]["surfaced"] > 0
    assert run["event"]["threshold"] >= 55
    for p in run["prospects"]:
        assert 0 <= p["fit_score"] <= 100
        assert p["fit_reason"]
        assert p["status"] in {"surfaced", "below", "approved", "contacted", "rsvp"}

    r = client.post(f"/events/{eid}/match")
    if r.status_code == 409:
        pytest.skip("no RSVPs in this seeded run — outreach funnel produced none")
    assert r.status_code == 200
    match = r.json()
    members = {m["id"]: m for g in match["groups"] for m in g["members"]}
    for e in match["edges"]:
        if e["edge_type"] == "symbiotic":
            assert members[e["a_id"]]["side"] != members[e["b_id"]]["side"]

    r = client.get(f"/events/{eid}/roi")
    assert r.status_code == 200
    roi = r.json()
    total_attendees = sum(g["builds"] + g["counterparts"] for g in match["groups"])
    assert len(roi["ledger"]) == total_attendees
    assert roi["metrics"]["budget"] == 9000
    assert roi["metrics"]["attended"] == total_attendees
    assert "net_roi_pct" in roi["metrics"]


def test_run_requires_real_event(client):
    assert client.post("/events/9999/run").status_code == 404


def test_match_before_run_is_conflict(client):
    eid = client.post("/events", json={"headcount": 12}).json()["id"]
    assert client.post(f"/events/{eid}/match").status_code == 409


# ===== split: /prospect, then /outreach ====================================

def test_split_prospect_then_outreach(client):
    eid = client.post("/events", json={
        "role": "Infra engineers", "seniority": "Senior", "co_stage": "Seed",
        "headcount": 9, "format": "Hackathon", "city": "SF",
        "goal": "Hiring pipeline", "budget": 9000,
    }).json()["id"]

    r = client.post(f"/events/{eid}/prospect")
    assert r.status_code == 200
    body = r.json()
    statuses = {p["status"] for p in body["prospects"]}
    assert statuses <= {"approved", "below"}
    assert "approved" in statuses
    for p in body["prospects"]:
        assert p["outreach"] == []

    r = client.post(f"/events/{eid}/outreach")
    assert r.status_code == 200
    body = r.json()
    statuses = {p["status"] for p in body["prospects"]}
    assert statuses <= {"below", "approved", "contacted", "rsvp"}
    assert any(p["status"] in ("contacted", "rsvp") for p in body["prospects"])


def test_outreach_before_prospect_is_conflict(client):
    eid = client.post("/events", json={"headcount": 12}).json()["id"]
    assert client.post(f"/events/{eid}/outreach").status_code == 409


def test_outreach_is_idempotent(client):
    eid = client.post("/events", json={"headcount": 9, "format": "Hackathon"}).json()["id"]
    client.post(f"/events/{eid}/prospect")
    r1 = client.post(f"/events/{eid}/outreach").json()
    r2 = client.post(f"/events/{eid}/outreach").json()
    logs1 = {p["id"]: len(p["outreach"]) for p in r1["prospects"]}
    logs2 = {p["id"]: len(p["outreach"]) for p in r2["prospects"]}
    assert logs1 == logs2


# ===== outreach: provider-backed dry-run + preview + log ====================

def _create_event_and_prospect(client, headcount=9):
    eid = client.post("/events", json={
        "role": "Infra engineers", "seniority": "Senior", "co_stage": "Seed",
        "headcount": headcount, "format": "Hackathon", "city": "SF",
        "goal": "Hiring pipeline", "budget": 9000,
    }).json()["id"]
    client.post(f"/events/{eid}/prospect")
    return eid


def test_outreach_dry_run_marks_dry_run_queued(client):
    eid = _create_event_and_prospect(client)
    body = client.post(f"/events/{eid}/outreach").json()
    assert body["dry_run"] is True
    assert body["provider"] == "unipile"
    states = {r["state"] for r in body["results"]}
    assert "dry_run_queued" in states
    for r in body["results"]:
        assert r["dry_run"] is True
        assert r["provider_lead_id"].startswith("dry_")


def test_outreach_preview_shows_messages_without_mutating(client):
    eid = _create_event_and_prospect(client)
    before = client.post(f"/events/{eid}/outreach").json()
    preview = client.get(f"/events/{eid}/outreach/preview").json()
    assert preview["provider"] == "unipile"
    assert preview["dry_run"] is True
    assert preview["count_eligible"] >= 1
    for row in preview["prospects"]:
        assert row["note"]
        assert row["note_chars"] == len(row["note"])
        assert row["note_chars"] <= 300
        assert row["message"]
        if row["eligible"]:
            assert row["payload"] is not None
            # Unipile payload shape: account_id + provider_id + message
            assert row["payload"]["message"]
        else:
            assert row["skip_reason"]
    after = client.get(f"/events/{eid}/prospects").json()
    before_statuses = {p["id"]: p["status"] for p in before["prospects"]}
    after_statuses = {p["id"]: p["status"] for p in after["prospects"]}
    assert before_statuses == after_statuses


def test_prospect_preview_runs_discovery_without_persisting(client):
    """The preview surfaces candidates AND shows the outreach note each one
    would receive, without writing a single row to the DB."""
    r = client.post("/events", json={})
    assert r.status_code in (200, 201)
    eid = r.json()["id"]

    # Nothing persisted before, nothing persisted after.
    with SessionLocal() as db:
        assert db.query(models.Prospect).count() == 0

    preview = client.get(f"/events/{eid}/prospect/preview").json()
    assert preview["event_id"] == eid
    assert preview["mode"] in ("llm", "mock")
    # No ANTHROPIC_API_KEY in the test env -> mock mode -> 22 mock candidates.
    assert preview["mode"] == "mock"
    assert preview["count"] >= 10

    with SessionLocal() as db:
        assert db.query(models.Prospect).count() == 0

    # Every candidate carries the LLM-extractable profile fields AND the
    # composed outreach note. This is the wire from discovery -> outreach.
    for c in preview["candidates"]:
        assert c["identity"]
        assert c["name"]
        assert c["note"]
        assert c["note_chars"] <= 300
        assert c["message"]
        # Peer reveal should mention at least one other surfaced candidate.
        if preview["count"] > 1:
            assert "are already in" in c["note"] or "is already in" in c["note"]


def test_outreach_log_endpoint_returns_timeline(client):
    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    log = client.get(f"/events/{eid}/outreach/log").json()
    assert log["event_id"] == eid
    assert log["count"] > 0
    seen_states = set()
    for e in log["entries"]:
        assert e["prospect_id"]
        assert e["state"]
        assert e["channel"] == "linkedin"
        assert e["provider"] == "unipile"
        seen_states.add(e["state"])
    assert "dry_run_queued" in seen_states


def test_outreach_skips_prospect_without_linkedin_url(client):
    eid = _create_event_and_prospect(client)
    db = SessionLocal()
    try:
        pros = db.query(models.Prospect).filter_by(
            event_id=eid, status="approved"
        ).first()
        assert pros is not None
        target_id = pros.id
        pros.linkedin_url = None
        db.commit()
    finally:
        db.close()

    client.post(f"/events/{eid}/outreach")
    log = client.get(f"/events/{eid}/outreach/log").json()
    failed = [e for e in log["entries"]
              if e["prospect_id"] == target_id and e["state"] == "failed"]
    assert failed, "skipped prospect should have a 'failed' log entry"
    assert "no linkedin_url" in failed[0]["body_preview"]


# ===== Unipile webhooks + auto-DM ==========================================

def _post_unipile_webhook(client, payload: dict, secret: str | None = None):
    body = json.dumps(payload).encode()
    headers = {}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["x-unipile-signature"] = f"sha256={sig}"
    return client.post("/webhooks/unipile", content=body, headers=headers)


def _outreached_prospect(eid: int) -> tuple[int, str]:
    """Return (prospect_id, linkedin_provider_id) for any prospect we sent
    outreach to (i.e., has a cached linkedin_provider_id)."""
    db = SessionLocal()
    try:
        p = db.query(models.Prospect).filter(
            models.Prospect.event_id == eid,
            models.Prospect.linkedin_provider_id.isnot(None),
        ).first()
        assert p is not None
        return p.id, p.linkedin_provider_id
    finally:
        db.close()


def _force_prospect_status(pid: int, status: str) -> None:
    db = SessionLocal()
    try:
        p = db.get(models.Prospect, pid)
        p.status = status
        db.commit()
    finally:
        db.close()


def test_webhook_new_relation_triggers_auto_dm(client):
    """The big one: new_relation webhook -> we auto-fire send_message."""
    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    pid, li_id = _outreached_prospect(eid)
    _force_prospect_status(pid, "contacted")

    r = _post_unipile_webhook(client, {
        "event": "new_relation",
        "timestamp": "2026-05-14T12:00:00Z",
        "user_provider_id": li_id,
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] is True
    assert out["state"] == "invite_accepted"
    assert out["prospect_id"] == pid
    assert out["auto_dm"] is not None
    assert out["auto_dm"]["state"] in ("dry_run_queued", "message_sent")
    assert out["auto_dm"]["dry_run"] is True

    log = client.get(f"/events/{eid}/outreach/log").json()
    p_entries = [e for e in log["entries"] if e["prospect_id"] == pid]
    states = [e["state"] for e in p_entries]
    assert "invite_accepted" in states
    # invite-time dry_run_queued + auto-DM dry_run_queued
    assert states.count("dry_run_queued") >= 2


def test_webhook_new_message_marks_rsvp(client):
    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    pid, li_id = _outreached_prospect(eid)
    _force_prospect_status(pid, "contacted")

    r = _post_unipile_webhook(client, {
        "event": "new_message",
        "user_provider_id": li_id,
        "message": {"text": "yes I'd love to come"},
        "timestamp": "2026-05-14T12:30:00Z",
    })
    assert r.status_code == 200
    assert r.json()["state"] == "message_replied"

    db = SessionLocal()
    try:
        p = db.get(models.Prospect, pid)
        assert p.status == "rsvp"
    finally:
        db.close()


def test_webhook_unknown_event_returns_200_no_mutation(client):
    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    log_before = client.get(f"/events/{eid}/outreach/log").json()["count"]

    r = _post_unipile_webhook(client, {"event": "ufo_sighted"})
    assert r.status_code == 200
    assert r.json()["applied"] is False

    log_after = client.get(f"/events/{eid}/outreach/log").json()["count"]
    assert log_before == log_after


def test_webhook_unknown_provider_id_no_crash(client):
    """Webhook for a LinkedIn user we don't have in our DB: 200, no mutation."""
    r = _post_unipile_webhook(client, {
        "event": "new_relation",
        "user_provider_id": "ACoAAA_unknown_to_us",
    })
    assert r.status_code == 200
    assert r.json()["applied"] is False


def test_webhook_is_idempotent(client):
    """Sending the same webhook event twice does not create duplicate state."""
    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    pid, li_id = _outreached_prospect(eid)
    _force_prospect_status(pid, "contacted")

    payload = {"event": "new_relation", "user_provider_id": li_id,
               "timestamp": "2026-05-14T12:00:00Z"}
    r1 = _post_unipile_webhook(client, payload)
    r2 = _post_unipile_webhook(client, payload)
    assert r1.json()["applied"] is True
    assert r2.json()["applied"] is False
    assert "duplicate" in r2.json()["reason"]


def test_webhook_signature_required_when_secret_set(client, monkeypatch):
    monkeypatch.setenv("UNIPILE_WEBHOOK_SECRET", "the-real-secret")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "true")
    reset_provider_cache()

    eid = _create_event_and_prospect(client)
    client.post(f"/events/{eid}/outreach")
    pid, li_id = _outreached_prospect(eid)
    _force_prospect_status(pid, "contacted")

    payload = {"event": "new_relation", "user_provider_id": li_id}

    # no signature → 401
    r = _post_unipile_webhook(client, payload, secret=None)
    assert r.status_code == 401

    # correct signature → 200
    r = _post_unipile_webhook(client, payload, secret="the-real-secret")
    assert r.status_code == 200
    assert r.json()["applied"] is True
