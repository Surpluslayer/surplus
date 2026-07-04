"""Regression: POST /events/{id}/check-connections is DETACHED.

The bulk connection-status refresh loops over every "unknown" prospect making
one Unipile call each. Run inline on a large approved pool it could exceed the
Cloudflare edge timeout (524) and pin a pooled DB connection across the whole
run. It now returns a fast 202 ack and hands the work to
pipeline._run_check_connections via jobs.run_detached, which runs on its OWN
session committing per prospect. The UI polls GET /events/{id}/prospects to
watch connection_status settle.

These tests assert:
  - the worker refreshes only unknown + linkedin_url prospects and commits
    once per prospect (freeing the pooled connection between provider calls),
  - the route authorizes, counts the work WITHOUT touching the network, and
    schedules exactly one background task instead of doing the loop inline.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
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


@pytest.fixture
def user_and_event(db):
    user = models.User(name="Op", email="op@x.com", unipile_account_id=None)
    db.add(user); db.flush()
    ev = models.Event(
        user_id=user.id, role="ML engineers", seniority="Senior",
        co_stage="Seed", headcount=20, format="Mixer", city="SF",
        goal="Hiring pipeline", budget=5000, sources="linkedin",
    )
    db.add(ev); db.commit()
    return user, ev


def _add_prospect(db, ev, *, name, status, linkedin_url):
    p = models.Prospect(
        event_id=ev.id, identity=name.lower(), name=name, role="x",
        company="x", seniority="Senior", side="Builds", works_on="x",
        offers="", seeks="", li_resolved=bool(linkedin_url),
        linkedin_url=linkedin_url,
        sources="linkedin", fit_score=80, status="approved",
        connection_status=status,
    )
    db.add(p); db.commit()
    return p


def test_worker_refreshes_only_unknown_with_url_and_commits_per_prospect(
    db, user_and_event, monkeypatch,
):
    _user, ev = user_and_event
    unk1 = _add_prospect(db, ev, name="Unk1", status="unknown",
                         linkedin_url="https://www.linkedin.com/in/unk1")
    unk2 = _add_prospect(db, ev, name="Unk2", status="unknown",
                         linkedin_url="https://www.linkedin.com/in/unk2")
    already = _add_prospect(db, ev, name="Known", status="connected",
                            linkedin_url="https://www.linkedin.com/in/known")
    no_url = _add_prospect(db, ev, name="NoUrl", status="unknown",
                           linkedin_url=None)

    # Fake provider : never hits the network, reports everyone connected.
    fake = SimpleNamespace(is_relation=lambda url: True)
    monkeypatch.setattr(pipeline, "get_preview_provider", lambda user: fake)

    # Count commits to prove we free the pooled connection between calls.
    commits = {"n": 0}
    real_commit = db.commit

    def _counting_commit():
        commits["n"] += 1
        real_commit()

    monkeypatch.setattr(db, "commit", _counting_commit)

    pipeline._run_check_connections(db, ev.id)

    db.expire_all()
    assert db.get(models.Prospect, unk1.id).connection_status == "connected"
    assert db.get(models.Prospect, unk2.id).connection_status == "connected"
    # Already-classified and url-less rows are left untouched.
    assert db.get(models.Prospect, already.id).connection_status == "connected"
    assert db.get(models.Prospect, no_url.id).connection_status == "unknown"
    assert db.get(models.Prospect, unk1.id).connection_checked_at is not None
    # One commit per refreshed prospect : NOT a single commit at the end.
    assert commits["n"] == 2


def test_worker_no_op_on_missing_event(db, monkeypatch):
    monkeypatch.setattr(pipeline, "get_preview_provider",
                        lambda user: SimpleNamespace(is_relation=lambda u: True))
    # Must not raise when the event id doesn't exist.
    pipeline._run_check_connections(db, 999999)


def test_route_returns_202_ack_and_schedules_without_network(
    db, user_and_event, monkeypatch,
):
    user, ev = user_and_event
    _add_prospect(db, ev, name="Unk1", status="unknown",
                  linkedin_url="https://www.linkedin.com/in/unk1")
    _add_prospect(db, ev, name="Unk2", status="unknown",
                  linkedin_url="https://www.linkedin.com/in/unk2")
    _add_prospect(db, ev, name="Known", status="connected",
                  linkedin_url="https://www.linkedin.com/in/known")
    _add_prospect(db, ev, name="NoUrl", status="unknown", linkedin_url=None)

    # If the handler touched the provider inline, this would blow up.
    def _boom(user):
        raise AssertionError("provider must not be resolved in the request path")

    monkeypatch.setattr(pipeline, "get_preview_provider", _boom)

    bg = BackgroundTasks()
    resp = pipeline.check_connections(ev.id, bg, db=db, user=user)

    assert resp["status"] == "scheduled"
    assert resp["event_id"] == ev.id
    assert resp["queued"] == 2          # two unknown + linkedin_url
    assert resp["skipped"] == 1         # one already-classified (connected)
    assert resp["runner"] == "local"
    assert resp["poll"] == f"/events/{ev.id}/prospects"

    # Exactly one detached task queued, wired to our worker : the loop did NOT
    # run inline (statuses are unchanged until the task executes off-path).
    assert len(bg.tasks) == 1
    db.expire_all()
    statuses = {p.name: p.connection_status for p in ev.prospects}
    assert statuses == {"Unk1": "unknown", "Unk2": "unknown",
                        "Known": "connected", "NoUrl": "unknown"}


def test_route_404s_for_unowned_event(db, user_and_event, monkeypatch):
    from fastapi import HTTPException

    _owner, ev = user_and_event
    other = models.User(name="Other", email="other@x.com", unipile_account_id=None)
    db.add(other); db.commit()

    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        pipeline.check_connections(ev.id, bg, db=db, user=other)
    assert exc.value.status_code == 404
    assert len(bg.tasks) == 0
