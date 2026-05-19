"""
Tests for PR E : operator accept/maybe/reject decision endpoint +
CSV export.

Exercises the route functions directly (same pattern as
test_triage_signup.py) so we sidestep the Python-3.9 union-syntax
issue in schemas.py that breaks the FastAPI app import locally.
"""
from __future__ import annotations
import csv
import io

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user_and_event(db):
    user = models.User(name="Op", email="op@example.com")
    db.add(user); db.commit(); db.refresh(user)
    event = models.Event(
        user_id=user.id, role="founders", seniority="[]", co_stage="[]",
        format="dinner", city="NYC", goal="[]", sources="[]",
        headcount=30, budget=0,
    )
    db.add(event); db.commit(); db.refresh(event)
    return user, event


def _make_applicant(db, event, *, name="Alice", with_eval=False,
                    fit=80, rec="accept"):
    a = models.Applicant(event_id=event.id, name=name, email=f"{name.lower()}@x.com")
    db.add(a); db.commit(); db.refresh(a)
    if with_eval:
        e = models.ApplicantEvaluation(
            applicant_id=a.id, event_id=event.id,
            fit_score=fit, confidence_score=70,
            recommendation=rec, archetype="founder",
            one_sentence_summary=f"{name} summary",
        )
        db.add(e); db.commit(); db.refresh(a)
    return a


# ── POST /decision ───────────────────────────────────────────────────

def test_set_decision_creates_row(db, user_and_event):
    user, event = user_and_event
    a = _make_applicant(db, event, with_eval=True, rec="accept")

    from backend.routes.triage import set_decision, DecisionBody
    result = set_decision(event.id, a.id,
                          DecisionBody(decision="accept", notes="great fit"),
                          db=db, user=user)
    assert result.decision is not None
    assert result.decision.human_decision == "accept"
    assert result.decision.reviewer_notes == "great fit"
    # System rec snapshot captured for override-rate analysis
    assert result.decision.system_recommendation == "accept"


def test_set_decision_is_upsert(db, user_and_event):
    user, event = user_and_event
    a = _make_applicant(db, event, with_eval=True, rec="accept")
    from backend.routes.triage import set_decision, DecisionBody
    set_decision(event.id, a.id, DecisionBody(decision="accept"),
                 db=db, user=user)
    set_decision(event.id, a.id, DecisionBody(decision="reject", notes="changed mind"),
                 db=db, user=user)
    rows = db.query(models.ReviewDecision).filter_by(applicant_id=a.id).all()
    assert len(rows) == 1
    assert rows[0].human_decision == "reject"
    assert rows[0].reviewer_notes == "changed mind"


def test_set_decision_rejects_invalid_value(db, user_and_event):
    user, event = user_and_event
    a = _make_applicant(db, event)
    from backend.routes.triage import set_decision, DecisionBody
    with pytest.raises(HTTPException) as exc:
        set_decision(event.id, a.id, DecisionBody(decision="kinda"),
                     db=db, user=user)
    assert exc.value.status_code == 400


def test_set_decision_rejects_cross_event_applicant(db, user_and_event):
    """Decision on an applicant that belongs to a different event must 404,
    not silently mutate the wrong row."""
    user, event = user_and_event
    other_event = models.Event(
        user_id=user.id, role="x", seniority="[]", co_stage="[]",
        format="dinner", city="SF", goal="[]", sources="[]",
        headcount=20, budget=0,
    )
    db.add(other_event); db.commit(); db.refresh(other_event)
    a = _make_applicant(db, other_event)

    from backend.routes.triage import set_decision, DecisionBody
    with pytest.raises(HTTPException) as exc:
        set_decision(event.id, a.id, DecisionBody(decision="accept"),
                     db=db, user=user)
    assert exc.value.status_code == 404


# ── GET /export.csv ──────────────────────────────────────────────────

def _read_csv_body(streaming_response) -> list[dict]:
    """StreamingResponse.body_iterator is an async-gen of chunks; drain it
    synchronously since our test fixture is sync. The route hands the iter
    a single-element list so this terminates fine."""
    import asyncio
    async def _drain():
        out = []
        async for chunk in streaming_response.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode())
        return out
    chunks = asyncio.new_event_loop().run_until_complete(_drain())
    return list(csv.DictReader(io.StringIO("".join(chunks))))


def test_export_includes_all_applicants_with_decisions(db, user_and_event):
    user, event = user_and_event
    a1 = _make_applicant(db, event, name="Alice", with_eval=True, fit=92, rec="accept")
    a2 = _make_applicant(db, event, name="Bob",   with_eval=True, fit=45, rec="reject")
    _make_applicant(db, event, name="Carl",  with_eval=False)  # unscored

    from backend.routes.triage import set_decision, DecisionBody, export_decisions_csv
    set_decision(event.id, a1.id, DecisionBody(decision="accept", notes="yes"),
                 db=db, user=user)
    set_decision(event.id, a2.id, DecisionBody(decision="reject"),
                 db=db, user=user)

    resp = export_decisions_csv(event.id, db=db, user=user)
    assert resp.media_type == "text/csv"
    rows = _read_csv_body(resp)
    assert {r["name"] for r in rows} == {"Alice", "Bob", "Carl"}

    alice = next(r for r in rows if r["name"] == "Alice")
    assert alice["fit_score"] == "92"
    assert alice["human_decision"] == "accept"
    assert alice["reviewer_notes"] == "yes"
    assert alice["system_recommendation"] == "accept"

    carl = next(r for r in rows if r["name"] == "Carl")
    # Unscored + undecided rows still appear so the operator sees the
    # whole pool, not just the reviewed slice.
    assert carl["human_decision"] == ""
    assert carl["fit_score"] == ""


def test_export_sorted_by_fit_desc(db, user_and_event):
    user, event = user_and_event
    _make_applicant(db, event, name="Low",  with_eval=True, fit=30)
    _make_applicant(db, event, name="High", with_eval=True, fit=95)
    _make_applicant(db, event, name="Mid",  with_eval=True, fit=60)

    from backend.routes.triage import export_decisions_csv
    rows = _read_csv_body(export_decisions_csv(event.id, db=db, user=user))
    assert [r["name"] for r in rows] == ["High", "Mid", "Low"]
