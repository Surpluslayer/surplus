"""tests/test_observe_routes.py : backend/routes/observe.py -- the
/api/observe/* HTTP surface. Real account auth (backend.auth.current_user via
FastAPI dependency override, same pattern test_demo_observability.py uses for
get_db), not the older ?key= token pattern -- this IS the account-aware layer
the Observe spec's success criterion #1 requires. Verifies both the "it
works" path and the authorization boundary: DEMO data is open to any
signed-in account, REAL data is strictly self-scoped, and no session at all
gets a 401.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend.auth import current_user
from backend.demo import cohort
from backend.main import app
from backend import models


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override_db

    db = Session()
    cohort_id = cohort.generate(db, n_lawyers=6, days=30)
    lawyer = db.execute(select(models.User).where(
        models.User.email == "demo-lawyer-000@example.com")).scalar_one()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == lawyer.id)).scalars().first()
    draft = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    lawyer_id, contact_id = lawyer.id, contact.id
    draft_id = draft.id if draft else None

    real_caller = models.User(email="real-caller@example.com", name="Real Caller", is_demo=False)
    db.add(real_caller)
    db.commit()
    db.refresh(real_caller)
    real_caller_id = real_caller.id
    db.close()

    def _current_real_caller():
        s = Session()
        try:
            return s.get(models.User, real_caller_id)
        finally:
            s.expunge_all()
            s.close()
    app.dependency_overrides[current_user] = _current_real_caller

    yield (TestClient(app), cohort_id, lawyer_id, contact_id,
           draft_id, Session, real_caller_id)
    app.dependency_overrides.clear()


def test_no_auth_is_401(client):
    c, _cohort_id, _lawyer_id, contact_id, _draft_id, _Session, _rid = client
    app.dependency_overrides.pop(current_user, None)
    r = c.get(f"/api/observe/trace/lawyer/{contact_id}")
    assert r.status_code == 401


def test_demo_data_is_visible_to_any_signed_in_account(client):
    c, _cohort_id, _lawyer_id, contact_id, _draft_id, _Session, _rid = client
    for path in ("trace/lawyer", "trace/relationship", "trace/opportunity", "trace/jurisdiction"):
        r = c.get(f"/api/observe/{path}/{contact_id}")
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"


def test_real_caller_cannot_see_another_real_users_contact(client):
    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    s = Session()
    other = models.User(email="other-real@example.com", name="Other Real", is_demo=False)
    s.add(other)
    s.commit()
    other_contact = models.Contact(user_id=other.id, primary_identity_key="x:1", name="Secret")
    s.add(other_contact)
    s.commit()
    s.refresh(other_contact)
    other_contact_id = other_contact.id
    s.close()

    r = c.get(f"/api/observe/trace/lawyer/{other_contact_id}")
    assert r.status_code == 404


def test_real_caller_can_see_their_own_data(client):
    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, real_caller_id = client
    s = Session()
    own_contact = models.Contact(user_id=real_caller_id, primary_identity_key="own:1",
                                 name="My Own Contact")
    s.add(own_contact)
    s.commit()
    s.refresh(own_contact)
    own_contact_id = own_contact.id
    s.close()

    r = c.get(f"/api/observe/trace/relationship/{own_contact_id}")
    assert r.status_code == 200
    assert r.json()["provenance"] == "observed"


def test_signal_draft_outcome_endpoints(client):
    c, _cohort_id, _lawyer_id, _contact_id, draft_id, _Session, _rid = client
    if draft_id is None:
        pytest.skip("cohort produced no draft interaction to test against")
    for path in ("trace/signal", "trace/draft", "trace/outcome"):
        r = c.get(f"/api/observe/{path}/{draft_id}")
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"


def test_all_five_harness_endpoints_plus_synthetic(client):
    c, cohort_id, _lawyer_id, _contact_id, _draft_id, _Session, _rid = client
    for path, params in (
        ("ablation", {"cohort_id": cohort_id}),
        ("relationship_evaluation", {"cohort_id": cohort_id}),
        ("signal_library_evaluation", {"cohort_id": cohort_id}),
        ("jurisdiction_regression", {}),
        ("historical_replay", {"cohort_id": cohort_id}),
        ("synthetic_scenarios", {}),
    ):
        r = c.get(f"/api/observe/harness/{path}", params=params)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"
        body = r.json()
        assert "cases_total" in body and "metrics" in body


def test_ablate_and_case_inspection_endpoints(client):
    c, _cohort_id, _lawyer_id, contact_id, _draft_id, _Session, _rid = client
    r = c.get(f"/api/observe/ablate/{contact_id}", params={"remove_group": "relationship"})
    assert r.status_code == 200
    body = r.json()
    assert "full_system" in body and "without_group" in body

    r2 = c.get(f"/api/observe/relationship_evaluation/case/{contact_id}")
    assert r2.status_code == 200
    assert "relationship_evidence" in r2.json()


def test_ablate_rejects_unknown_group_as_400(client):
    c, _cohort_id, _lawyer_id, contact_id, _draft_id, _Session, _rid = client
    r = c.get(f"/api/observe/ablate/{contact_id}", params={"remove_group": "not_real"})
    assert r.status_code == 400


def test_cohorts_and_book_endpoints(client):
    c, cohort_id, _lawyer_id, _contact_id, _draft_id, _Session, _rid = client
    r = c.get("/api/observe/cohorts")
    assert r.status_code == 200
    assert cohort_id in [row["cohort_id"] for row in r.json()["cohorts"]]

    r2 = c.get("/api/observe/book", params={"cohort_id": cohort_id,
                                             "user_email": "demo-lawyer-000@example.com"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["user"]["email"] == "demo-lawyer-000@example.com"
    assert "opportunities" in body


def test_book_endpoint_rejects_another_real_users_email(client):
    c, cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    s = Session()
    other = models.User(email="not-mine@example.com", name="Not Mine", is_demo=False)
    s.add(other)
    s.commit()
    s.close()
    r = c.get("/api/observe/book", params={"cohort_id": cohort_id, "user_email": "not-mine@example.com"})
    assert r.status_code == 404


def test_activity_stream_replays_only_new_events_and_resumes(client, monkeypatch):
    """/stream/activity is the endpoint the ask-bar narration reaches the UI
    through. Two properties matter:

      - a fresh reader starts at the tip (it must not dump the whole
        retention window into the log on every page load), and
      - a reconnecting reader resumes from Last-Event-ID, so bounding the
        stream's lifetime cannot silently drop lines.
    """
    from backend.observe import bus

    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, rid = client
    bus.use_session_factory(Session)
    monkeypatch.setattr("backend.routes.observe._ACTIVITY_STREAM_MAX_S", 1.5)
    try:
        bus.publish(rid, "info", "src", "BEFORE the reader connected")
        tip = bus.latest_seq()

        with c.stream("GET", "/api/observe/stream/activity") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "BEFORE the reader connected" not in body, "replayed stale history"

        bus.publish(rid, "info", "src", "AFTER, while disconnected")
        with c.stream("GET", "/api/observe/stream/activity",
                      headers={"Last-Event-ID": str(tip)}) as r:
            body = "".join(r.iter_text())
        assert "AFTER, while disconnected" in body, "resume dropped an event"
    finally:
        bus.use_session_factory(None)


def test_activity_stream_does_not_pin_a_pool_connection(client, monkeypatch):
    """The request-scoped session must be released before streaming starts.

    An endless generator never lets FastAPI tear its dependencies down, so a
    stream that kept the get_db session would burn one pooled connection per
    open Observe tab until the pool was exhausted.
    """
    from backend.observe import bus

    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    bus.use_session_factory(Session)
    monkeypatch.setattr("backend.routes.observe._ACTIVITY_STREAM_MAX_S", 1.0)

    closed = {"n": 0}
    real_close = Session.class_.close

    def counting_close(self):
        closed["n"] += 1
        return real_close(self)

    monkeypatch.setattr(Session.class_, "close", counting_close)
    try:
        with c.stream("GET", "/api/observe/stream/activity") as r:
            "".join(r.iter_text())
        assert closed["n"] > 0, "the request-scoped session was never closed"
    finally:
        bus.use_session_factory(None)
