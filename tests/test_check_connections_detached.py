"""POST /events/{id}/check-connections is DETACHED: it schedules a worker via
run_detached and returns 202 + counts (so a large approved pool can't 524 the
request or pin a pooled connection). The worker refreshes only unknown+linkedin
prospects, committing per prospect.

Direct-call style (no TestClient/auth), matching test_connection_routing.py.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import pipeline


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _p(ev_id, name, status, li):
    return models.Prospect(
        event_id=ev_id, identity=name.lower(), name=name, role="x", company="x",
        seniority="Staff+", side="Builds", works_on="x", offers="", seeks="",
        li_resolved=bool(li), linkedin_url=li, sources="linkedin",
        fit_score=80, status="surfaced", connection_status=status)


def _seed(db):
    u = models.User(email="h@x.com", name="Host")
    db.add(u); db.commit(); db.refresh(u)
    ev = models.Event(user_id=u.id, role="x", seniority="Staff+", co_stage="Seed",
                      headcount=40, format="Dinner", city="SF", goal="g",
                      budget=8000, threshold=70)
    db.add(ev); db.commit(); db.refresh(ev)
    db.add_all([
        _p(ev.id, "A", "unknown", "https://linkedin.com/in/a"),      # -> queued
        _p(ev.id, "B", "unknown", "https://linkedin.com/in/b"),      # -> queued
        _p(ev.id, "C", "connected", "https://linkedin.com/in/c"),    # -> skipped
        _p(ev.id, "D", "unknown", None),                             # -> neither
    ])
    db.commit()
    return u, ev


def test_route_returns_202_counts_and_schedules(db, monkeypatch):
    u, ev = _seed(db)
    sched = []
    monkeypatch.setattr(pipeline, "run_detached",
                        lambda fn, *a: sched.append((fn, a)) or "local")
    monkeypatch.setattr(pipeline, "get_owned_event", lambda eid, user, d: ev)

    res = pipeline.check_connections(ev.id, db=db, user=SimpleNamespace(id=u.id))

    assert res["status"] == "scheduled"
    assert res["queued"] == 2 and res["skipped"] == 1     # A,B queued; C skipped; D neither
    # scheduled the worker with just the event id (main's run_detached(fn,*args))
    assert sched and sched[0][0] is pipeline._run_check_connections
    assert sched[0][1] == (ev.id,)
    # nothing was refreshed inline (no network in the request)
    assert {p.connection_status for p in ev.prospects} == {"unknown", "connected"}


def test_worker_refreshes_only_unknown_with_linkedin(db, monkeypatch):
    u, ev = _seed(db)
    refreshed = []

    def _fake_refresh(provider, p):
        refreshed.append(p.name)
        p.connection_status = "connected"
        return "connected"

    monkeypatch.setattr(pipeline, "_refresh_connection_status", _fake_refresh)
    monkeypatch.setattr(pipeline, "get_preview_provider", lambda owner: object())

    pipeline._run_check_connections(db, ev.id)

    assert sorted(refreshed) == ["A", "B"]                # not C (connected) or D (no linkedin)
    db.expire_all()
    by_name = {p.name: p.connection_status for p in ev.prospects}
    assert by_name["A"] == "connected" and by_name["B"] == "connected"
    assert by_name["D"] == "unknown"                       # untouched
