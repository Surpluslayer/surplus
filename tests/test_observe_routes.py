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


def _track_get_db_session(app, Session):
    """A get_db override that records the id() of the session it hands out,
    so a test can ask "was THIS SPECIFIC session closed", not "was any
    session anywhere closed". That distinction matters here because the
    fixture's own current_user override (_current_real_caller) creates and
    immediately closes its OWN unrelated session on every call -- a naive
    global close-counter would show "closed" from that alone and pass
    whether or not the route under test does anything right."""
    from backend.db import get_db
    session_ids = set()

    def _override():
        s = Session()
        session_ids.add(id(s))
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override
    return session_ids


def test_boot_stream_releases_the_request_scoped_session_before_streaming(client, monkeypatch):
    """stream/activity got this fix (2a0583f); stream/boot and stream/contact
    inherit the SAME risk from the SAME cause -- Depends(current_user)
    resolves through Depends(get_db), and FastAPI caches that per request, so
    an explicit db: Depends(get_db) parameter on the route resolves to the
    SAME session current_user already opened. FastAPI holds a yield-based
    dependency's session open until the response completes, which for an SSE
    endpoint is the whole stream -- unless the route closes it itself, which
    is exactly what this proves happened BEFORE the harness suite's first
    line, not merely by the time the response happens to finish."""
    from backend.observe import logstream

    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    tracked_ids = _track_get_db_session(app, Session)

    closed_ids = set()
    real_close = Session.class_.close

    def counting_close(self):
        closed_ids.add(id(self))
        return real_close(self)
    monkeypatch.setattr(Session.class_, "close", counting_close)

    closed_before_first_line = {"v": None}
    real_boot_events = logstream.boot_events

    def spying_boot_events(db, user=None):
        closed_before_first_line["v"] = bool(tracked_ids & closed_ids)
        yield from real_boot_events(db, user)
    monkeypatch.setattr(logstream, "boot_events", spying_boot_events)

    with c.stream("GET", "/api/observe/stream/boot") as r:
        assert r.status_code == 200
        "".join(r.iter_text())
    assert tracked_ids, "the route never resolved a get_db session at all"
    assert closed_before_first_line["v"] is True, \
        "request-scoped session was still open when the harness suite started"


def test_boot_stream_sends_done_after_an_error_so_the_browser_stops_reconnecting(client, monkeypatch):
    """EventSource treats any server-closed connection it didn't request via
    .close() as dropped and auto-reconnects, reissuing the same GET -- the
    frontend's "done" listener is the ONLY thing that calls es.close(). If an
    exception mid-stream ends the generator with just "event: error" and no
    "event: done", the browser silently reruns the whole harness suite,
    forever, instead of surfacing the failure once."""
    from backend.observe import logstream

    c, _cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client

    def broken_boot_events(db, user=None):
        yield from []
        raise RuntimeError("boom")
        yield  # pragma: no cover - unreachable, keeps this a generator
    monkeypatch.setattr(logstream, "boot_events", broken_boot_events)

    with c.stream("GET", "/api/observe/stream/boot") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "event: error" in body
    assert "event: done" in body
    assert body.index("event: error") < body.index("event: done")


def test_contact_stream_releases_the_request_scoped_session_before_streaming(client, monkeypatch):
    """Same property as the boot-stream test above, for stream/contact/{id}
    -- the HIGHEST-volume of the three Observe streams, since it fires on
    every card clicked in the Book, not once per opened tab."""
    from backend.observe import logstream

    c, _cohort_id, _lawyer_id, contact_id, _draft_id, Session, _rid = client
    # _sse()'s generator opens its OWN session via `from ..db import
    # SessionLocal; wdb = SessionLocal()` -- the request-scoped `db` this
    # test is exercising is a SEPARATE thing entirely. Without pointing the
    # module-level SessionLocal at this test's isolated engine, wdb.get(...)
    # inside stream_contact's events() resolves against the real default
    # database, finds nothing, and takes the early-return branch -- which
    # means contact_events (and this test's spy on it) never runs at all.
    monkeypatch.setattr("backend.db.SessionLocal", Session)
    tracked_ids = _track_get_db_session(app, Session)

    closed_ids = set()
    real_close = Session.class_.close

    def counting_close(self):
        closed_ids.add(id(self))
        return real_close(self)
    monkeypatch.setattr(Session.class_, "close", counting_close)

    closed_before_first_line = {"v": None}
    real_contact_events = logstream.contact_events

    def spying_contact_events(db, user, contact, channel="linkedin_dm"):
        closed_before_first_line["v"] = bool(tracked_ids & closed_ids)
        yield from real_contact_events(db, user, contact, channel)
    monkeypatch.setattr(logstream, "contact_events", spying_contact_events)

    with c.stream("GET", f"/api/observe/stream/contact/{contact_id}") as r:
        assert r.status_code == 200
        "".join(r.iter_text())
    assert tracked_ids, "the route never resolved a get_db session at all"
    assert closed_before_first_line["v"] is True, \
        "request-scoped session was still open when the contact trace started"


# ── cohort-level authorization (a REAL, non-demo cohort -- e.g. one seeded by
# backend/demo/seed_operator.py --unipile-users -- must be as self-scoped as
# a single contact is; see routes/observe.py's _cohort_visible_to) ─────────

def _tag_real_cohort(Session, cohort_id: str, *, owner_email: str):
    """A minimal stand-in for what seed_operator.seed_from_unipile_users now
    produces: a REAL (is_demo=False) user, a contact, and a users-table
    DemoProvenance tag -- the exact shape cohort_query.users_and_contacts
    needs to resolve a lawyer for this cohort."""
    from backend.demo import provenance as prov
    s = Session()
    owner = models.User(email=owner_email, name="Real Owner", is_demo=False)
    s.add(owner)
    s.commit()
    s.refresh(owner)
    contact = models.Contact(user_id=owner.id, primary_identity_key="real:1", name="Real Client")
    s.add(contact)
    s.commit()
    s.refresh(contact)
    prov.tag(s, owner, provenance=prov.BASELINE, cohort_id=cohort_id)
    prov.tag(s, contact, provenance=prov.BASELINE, cohort_id=cohort_id)
    s.commit()
    owner_id = owner.id
    s.close()
    return owner_id


def test_real_cohort_is_invisible_to_a_different_real_caller(client):
    """Without cohort-level authorization, any signed-in account could pass
    an arbitrary cohort_id to a harness/trace route and read back another
    real lawyer's aggregated book -- no per-contact check ever runs, because
    these routes never resolve a single owned contact the way trace/lawyer
    etc. do."""
    c, _demo_cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    real_cohort_id = "unipile-real-cohort"
    _tag_real_cohort(Session, real_cohort_id, owner_email="owner@firm.com")

    for path, params in (
        ("harness/ablation", {"cohort_id": real_cohort_id}),
        ("harness/relationship_evaluation", {"cohort_id": real_cohort_id}),
        ("harness/signal_library_evaluation", {"cohort_id": real_cohort_id}),
        ("harness/historical_replay", {"cohort_id": real_cohort_id}),
        ("trace/signal_library/gc_appointment", {"cohort_id": real_cohort_id}),
    ):
        r = c.get(f"/api/observe/{path}", params=params)
        assert r.status_code == 404, f"{path} -> {r.status_code}: {r.text}"

    # And it must not even be listed as an option to a caller who can't see it.
    r = c.get("/api/observe/cohorts")
    assert real_cohort_id not in [row["cohort_id"] for row in r.json()["cohorts"]]


def test_real_cohort_is_visible_to_its_own_owner(client):
    c, _demo_cohort_id, _lawyer_id, _contact_id, _draft_id, Session, _rid = client
    real_cohort_id = "unipile-real-cohort"
    owner_id = _tag_real_cohort(Session, real_cohort_id, owner_email="owner2@firm.com")

    def _current_owner():
        s = Session()
        try:
            return s.get(models.User, owner_id)
        finally:
            s.expunge_all()
            s.close()
    app.dependency_overrides[current_user] = _current_owner

    r = c.get("/api/observe/cohorts")
    assert real_cohort_id in [row["cohort_id"] for row in r.json()["cohorts"]]

    # Reaches real harness logic instead of being rejected at the auth gate --
    # not asserting 200, since a 1-contact fixture may not satisfy every
    # harness's own data requirements, only that it isn't the 404 this test
    # exists to distinguish from.
    r = c.get("/api/observe/harness/signal_library_evaluation",
              params={"cohort_id": real_cohort_id})
    assert r.status_code != 404, r.text
