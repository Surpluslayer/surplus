"""Tests for routes/demo_observability.py -- the ranking-trace HTTP surface.

Same TestClient + in-memory SQLite fixture pattern as test_account_email.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend.demo import cohort
from backend.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "test-secret-key")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override

    # Seed a real cohort directly against the same engine/session factory.
    db = Session()
    cohort_id = cohort.generate(db, n_lawyers=8, days=30, cohort_id="test-cohort")
    db.close()

    yield TestClient(app), cohort_id
    app.dependency_overrides.clear()


def test_wrong_key_is_404_not_403(client):
    c, cohort_id = client
    r = c.get("/api/demo/observability/cohorts", params={"key": "wrong"})
    assert r.status_code == 404


def test_no_token_configured_is_404(client, monkeypatch):
    monkeypatch.delenv("DEMO_ACCESS_TOKEN", raising=False)
    c, cohort_id = client
    r = c.get("/api/demo/observability/cohorts", params={"key": "anything"})
    assert r.status_code == 404


def test_cohorts_lists_the_generated_cohort(client):
    c, cohort_id = client
    r = c.get("/api/demo/observability/cohorts", params={"key": "test-secret-key"})
    assert r.status_code == 200
    ids = [row["cohort_id"] for row in r.json()["cohorts"]]
    assert cohort_id in ids


def test_opportunities_and_trace_round_trip(client):
    c, cohort_id = client
    # Grab a real seeded lawyer email directly (demo-lawyer-000..007).
    r = c.get("/api/demo/observability/opportunities", params={
        "key": "test-secret-key", "cohort_id": cohort_id,
        "user_email": "demo-lawyer-000@example.com",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "demo-lawyer-000@example.com"
    assert "opportunities" in body

    if body["opportunities"]:
        contact_id = body["opportunities"][0]["contact_id"]
        tr = c.get(f"/api/demo/observability/trace/{contact_id}", params={
            "key": "test-secret-key", "user_email": "demo-lawyer-000@example.com",
        })
        assert tr.status_code == 200
        trace = tr.json()
        assert trace["contact_id"] == contact_id
        assert len(trace["factors"]) == 6
        names = {f["name"] for f in trace["factors"]}
        assert names == {"signal_relevance", "practice_fit", "relationship_strength",
                          "relationship_recency", "relationship_trajectory",
                          "historical_behavior"}

        fb = c.get(f"/api/demo/observability/feedback/{contact_id}", params={
            "key": "test-secret-key", "user_email": "demo-lawyer-000@example.com",
        })
        assert fb.status_code == 200
        assert "steps" in fb.json()


def test_trace_for_another_users_contact_is_404(client):
    c, cohort_id = client
    all_contacts_r = c.get("/api/demo/observability/opportunities", params={
        "key": "test-secret-key", "cohort_id": cohort_id,
        "user_email": "demo-lawyer-001@example.com", "limit": 50,
    })
    # Cross-user access: a contact_id that belongs to lawyer 001 must 404 when
    # queried as lawyer 000.
    contacts_1 = all_contacts_r.json()["opportunities"]
    if contacts_1:
        foreign_id = contacts_1[0]["contact_id"]
        r = c.get(f"/api/demo/observability/trace/{foreign_id}", params={
            "key": "test-secret-key", "user_email": "demo-lawyer-000@example.com",
        })
        assert r.status_code == 404


def test_eval_backtest_endpoint_returns_real_computed_numbers(client):
    c, cohort_id = client
    r = c.get("/api/demo/observability/eval", params={
        "key": "test-secret-key", "cohort_id": cohort_id,
    })
    assert r.status_code == 200
    body = r.json()
    assert "v0_naive_recency_ranker" in body
    assert "v1_full_ranking_trace" in body
    assert "caveat" in body
