"""
Route tests for the relationship read API (routes/relationships.py) and the
CRM capture-row enrichment in routes/inperson.py.

Repo convention : call route functions directly with an in-memory SQLAlchemy
session + real ORM rows. No TestClient / auth cookies; UNIPILE_DRY_RUN on.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import relationships as rel_route
from backend.routes.inperson import _capture_row


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Op"), email=kw.get("email", "op@x.com"),
                    unipile_account_id=kw.get("acct", "acct1"))
    db.add(u); db.commit()
    return u


def _captured_prospect(db, user, **kw):
    ev = models.Event(user_id=user.id, kind="in_person", label="Mixer", city="SF")
    db.add(ev); db.commit()
    p = models.Prospect(
        event_id=ev.id, identity="maya", name="Maya Rodriguez",
        role="Staff Infra", company="Lo91r",
        linkedin_url="https://linkedin.com/in/maya",
        status="pending", source="scan",
        captured_at=datetime.now(timezone.utc),
        note=kw.get("note"), private_note=kw.get("private_note"),
        contact_type=kw.get("contact_type"), next_step=kw.get("next_step"),
    )
    db.add(p); db.commit()
    return ev, p


# ── timeline endpoint ───────────────────────────────────────────────────

def test_timeline_endpoint_shape(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, note="talked KV-cache", contact_type="sponsor")
    db.add(models.OutreachLog(prospect_id=p.id, channel="linkedin",
                              state="invite_sent")); db.commit()

    out = rel_route.prospect_timeline(p.id, db, u)
    assert set(out) == {"prospect", "relationship_summary", "timeline"}
    assert out["prospect"]["prospect_id"] == p.id
    assert out["prospect"]["name"] == "Maya Rodriguez"
    assert out["relationship_summary"]["relationship_stage"] == "contacted"
    assert out["relationship_summary"]["contact_type"] == "sponsor"
    # timeline carries capture + note + outreach
    kinds = {it["source_type"] for it in out["timeline"]}
    assert {"in_person_capture", "manual_note", "linkedin_outreach"} <= kinds


def test_timeline_unauthorized_prospect_blocked(db):
    owner = _user(db, email="owner@x.com", acct="owner_acct")
    other = _user(db, email="other@x.com", acct="other_acct")
    ev, p = _captured_prospect(db, owner)
    # A different user must not be able to read the owner's prospect.
    with pytest.raises(HTTPException) as ei:
        rel_route.prospect_timeline(p.id, db, other)
    assert ei.value.status_code == 404


def test_timeline_missing_prospect_404(db):
    u = _user(db)
    with pytest.raises(HTTPException) as ei:
        rel_route.prospect_timeline(99999, db, u)
    assert ei.value.status_code == 404


def test_timeline_prospect_brief_does_not_leak_private_note(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, private_note="budget approver")
    out = rel_route.prospect_timeline(p.id, db, u)
    # The compact header subset must not carry the private note text.
    assert "budget approver" not in str(out["prospect"])


# ── /captures enrichment (backward compatibility) ─────────────────────────

def test_capture_row_keeps_existing_fields_and_adds_summary(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, next_step="send deck")
    row = _capture_row(p)
    # Existing fields preserved (not renamed/removed).
    for key in ("prospect_id", "name", "role", "company", "linkedin_url",
                "status", "connection_status", "source", "captured_at",
                "note", "private_note", "contact_type", "next_step",
                "resolve_failed", "last_outreach", "conversion"):
        assert key in row
    # New additive field.
    assert "relationship_summary" in row
    assert row["relationship_summary"]["next_step"] == "send deck"
    assert row["relationship_summary"]["relationship_stage"] == "captured"


# ── list endpoint (all relationships across events) ───────────────────────

def _captured_at_event(db, user, label, name, captured_at, **kw):
    ev = models.Event(user_id=user.id, kind="in_person", label=label, city="SF")
    db.add(ev); db.commit()
    p = models.Prospect(
        event_id=ev.id, identity=name.lower(), name=name, role="Eng",
        company="Co", linkedin_url=f"https://linkedin.com/in/{name.lower()}",
        status="pending", source="scan", captured_at=captured_at,
        contact_type=kw.get("contact_type"),
    )
    db.add(p); db.commit()
    return ev, p


def test_list_returns_all_user_relationships_across_events(db):
    u = _user(db)
    _captured_at_event(db, u, "Dinner", "Maya", datetime.now(timezone.utc))
    _captured_at_event(db, u, "Mixer", "Sam", datetime.now(timezone.utc))
    out = rel_route.list_relationships(db=db, user=u)
    assert out["count"] == 2
    names = {r["prospect"]["name"] for r in out["relationships"]}
    assert names == {"Maya", "Sam"}


def test_list_is_owner_scoped(db):
    owner = _user(db, email="owner@x.com", acct="o")
    other = _user(db, email="other@x.com", acct="x")
    _captured_at_event(db, owner, "Dinner", "Maya", datetime.now(timezone.utc))
    out = rel_route.list_relationships(db=db, user=other)
    assert out["count"] == 0


def test_list_filters_by_event(db):
    u = _user(db)
    ev1, _ = _captured_at_event(db, u, "Dinner", "Maya", datetime.now(timezone.utc))
    _captured_at_event(db, u, "Mixer", "Sam", datetime.now(timezone.utc))
    out = rel_route.list_relationships(event_id=ev1.id, db=db, user=u)
    assert out["count"] == 1
    assert out["relationships"][0]["prospect"]["name"] == "Maya"


def test_list_filters_by_stage(db):
    u = _user(db)
    _, p = _captured_at_event(db, u, "Dinner", "Maya", datetime.now(timezone.utc))
    _captured_at_event(db, u, "Mixer", "Sam", datetime.now(timezone.utc))
    # Maya gets outreach -> "contacted"; Sam stays "captured".
    db.add(models.OutreachLog(prospect_id=p.id, channel="linkedin",
                              state="invite_sent")); db.commit()
    out = rel_route.list_relationships(stage="contacted", db=db, user=u)
    assert out["count"] == 1
    assert out["relationships"][0]["prospect"]["name"] == "Maya"


def test_list_filters_by_contact_type(db):
    u = _user(db)
    _captured_at_event(db, u, "Dinner", "Maya", datetime.now(timezone.utc),
                       contact_type="sponsor")
    _captured_at_event(db, u, "Mixer", "Sam", datetime.now(timezone.utc))
    out = rel_route.list_relationships(contact_type="sponsor", db=db, user=u)
    assert out["count"] == 1
    assert out["relationships"][0]["prospect"]["name"] == "Maya"


def test_list_sorted_newest_touch_first(db):
    u = _user(db)
    _captured_at_event(db, u, "Old", "Older",
                       datetime.now(timezone.utc) - timedelta(days=10))
    _captured_at_event(db, u, "New", "Newer",
                       datetime.now(timezone.utc) - timedelta(days=1))
    out = rel_route.list_relationships(db=db, user=u)
    assert [r["prospect"]["name"] for r in out["relationships"]] == ["Newer", "Older"]


# ── import-conversations : queued job + poll ─────────────────────────────

def test_import_conversations_queues_job_and_polls(db, monkeypatch):
    """POST /import-conversations no longer runs the (minutes-long) Unipile
    chat walk inline : it queues a Job and returns the id immediately. The
    detached worker (run here explicitly, as the thread would, with the walk
    stubbed) writes progress beats and the final stats onto the Job row for
    GET /import-conversations/{job_id}."""
    from backend import jobs as jobs_mod

    u = _user(db)

    dispatched = []
    monkeypatch.setattr(
        jobs_mod, "run_detached",
        lambda fn, *a, prefer_modal=False, **k: (dispatched.append((fn, a, k)),
                                                 "local")[1])

    out = rel_route.import_conversations(want=5, db=db, user=u)
    assert out["status"] == "queued" and out["job_id"]
    job_id = out["job_id"]

    # Queued, no progress yet.
    poll = rel_route.import_conversations_status(job_id, db, u)
    assert poll == {"job_id": job_id, "status": "queued"}

    # Run the worker with the Unipile walk stubbed : it should surface the
    # on_progress beats and then the final stats.
    def _fake_import(session, user, want=15, on_progress=None):
        assert want == 5
        if on_progress:
            on_progress(7, 2)
        return {"imported": 2, "considered": 3}
    import backend.jobs as backend_jobs
    monkeypatch.setattr(
        "backend.agents.relationship.spine.relationships."
        "import_conversation_contacts", _fake_import)

    (fn, args, kwargs) = dispatched[0]
    assert fn is backend_jobs.execute_import_conversations
    fn(db, *args, **kwargs)

    poll = rel_route.import_conversations_status(job_id, db, u)
    assert poll["status"] == "done"
    assert poll["result"] == {"imported": 2, "considered": 3}


def test_import_conversations_poll_is_owner_scoped(db, monkeypatch):
    from backend import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "run_detached",
                        lambda *a, **k: "local")
    u = _user(db)
    other = _user(db, name="Other", email="o@x.com", acct="acct2")
    out = rel_route.import_conversations(db=db, user=u)
    with pytest.raises(HTTPException) as ei:
        rel_route.import_conversations_status(out["job_id"], db, other)
    assert ei.value.status_code == 404
