"""
Tests for triage routes (POST config / POST upload / GET applicants).

Exercises the route functions directly with an in-memory SQLAlchemy
session : same workaround test_followups.py uses to avoid the Python
3.9 / str|None evaluation issue when importing FastAPI's app.
"""
from __future__ import annotations
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
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
    """Real User + Event rows : routes need ownership to pass get_owned_event."""
    user = models.User(name="Operator", email="op@example.com", unipile_account_id=None)
    db.add(user); db.flush()
    ev = models.Event(
        user_id=user.id,
        role="(triage event)", seniority="", co_stage="",
        headcount=40, format="Sit-down dinner", city="NYC",
        goal="", budget=0, sources="linkedin",
    )
    db.add(ev); db.commit()
    return user, ev


def _upload_file(content: str | bytes, filename="luma.csv",
                 content_type="text/csv") -> UploadFile:
    """Build a FastAPI UploadFile around an in-memory CSV string."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    f = io.BytesIO(content)
    return UploadFile(filename=filename, file=f,
                      headers={"content-type": content_type})


# ── config ─────────────────────────────────────────────────────────────

def test_set_and_get_config_roundtrips(db, user_and_event):
    from backend.routes.triage import set_triage_config, get_triage_config, TriageConfig
    user, ev = user_and_event
    body = TriageConfig(
        event_type="sponsor_cafe", sponsor_name="Stripe x ElevenLabs",
        event_goal="builders with high-transaction products",
        ideal_attendee_profile="founders shipping consumer AI",
        hard_filters=["Must be in NYC"],
        nice_to_have_signals=["High monthly transactions"],
        anti_fit_examples=["Photography businesses"],
        capacity=30, notes="this is a test",
    )
    set_triage_config(ev.id, body, db, user)
    db.refresh(ev)
    # Round-trip through GET
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name == "Stripe x ElevenLabs"
    assert got.hard_filters == ["Must be in NYC"]
    assert got.capacity == 30


def test_get_config_returns_empty_for_unset_event(db, user_and_event):
    from backend.routes.triage import get_triage_config
    user, ev = user_and_event
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name is None
    assert got.hard_filters == []


def test_get_config_returns_empty_on_corrupted_json(db, user_and_event):
    """A bad triage_config string shouldn't 500 the UI : return empty,
    operator can re-save."""
    from backend.routes.triage import get_triage_config
    user, ev = user_and_event
    ev.triage_config = "not valid json {"
    db.commit()
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name is None


# ── upload ─────────────────────────────────────────────────────────────

def _bg_tasks():
    """A real BackgroundTasks instance whose add_task is a no-op for tests :
    we don't want the route to actually fire the Anthropic-backed evaluator
    in unit tests."""
    from fastapi import BackgroundTasks
    bt = BackgroundTasks()
    bt.add_task = lambda *args, **kwargs: None  # type: ignore
    return bt


def test_upload_persists_applicants(db, user_and_event):
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    csv = (
        "Name,Email,Job Title,Company,LinkedIn URL,Are you a creator?\n"
        "Maya,m@x.com,Staff Infra,Lo91r,https://linkedin.com/in/maya,no\n"
        "Theo,t@x.com,Distrib Sys,Fly.io,https://linkedin.com/in/theo,yes\n"
    )
    result = upload_applicants(ev.id, _bg_tasks(), _upload_file(csv), db, user)
    assert result.parsed == 2
    assert result.inserted == 2
    assert result.evaluation_started is True
    db.refresh(ev)
    assert len(ev.applicants) == 2
    maya = next(a for a in ev.applicants if a.name == "Maya")
    assert maya.email == "m@x.com"
    assert maya.linkedin_url == "https://linkedin.com/in/maya"
    raw = json.loads(maya.raw_application_data)
    assert raw["Are you a creator?"] == "no"


def test_upload_clips_overlong_fields_to_column_limits(db, user_and_event):
    """Luma maps free-text survey answers ('what do you do') into role/company,
    which can exceed VARCHAR(200). Postgres rejects the INSERT (hard 500);
    SQLite silently accepts it. We clip the mapped, length-capped columns on
    write so prod doesn't 500, while unmapped free-text columns are retained
    verbatim in raw_application_data."""
    from backend.routes.triage import upload_applicants, _APPLICANT_COL_MAXLEN
    user, ev = user_and_event
    long_role = "Founder and CEO — " + ("building AI-native event ops " * 20)
    long_company = "A" * 400
    long_name = "N" * 300
    long_answer = "B" * 5000  # unmapped free-text column → kept verbatim
    assert len(long_role) > 200 and len(long_company) > 200 and len(long_name) > 160
    csv = (
        "Name,Email,Job Title,Company,What are you building?\n"
        f"{long_name},who@x.com,{long_role},{long_company},{long_answer}\n"
    )
    result = upload_applicants(ev.id, _bg_tasks(), _upload_file(csv), db, user)
    assert result.inserted == 1
    db.refresh(ev)
    a = ev.applicants[0]
    assert len(a.name) == _APPLICANT_COL_MAXLEN["name"] == 160
    assert len(a.role) == _APPLICANT_COL_MAXLEN["role"] == 200
    assert len(a.company) == _APPLICANT_COL_MAXLEN["company"] == 200
    # Unmapped free-text survey answer preserved verbatim in the TEXT column.
    raw = json.loads(a.raw_application_data)
    assert raw["What are you building?"] == long_answer


def test_upload_rejects_non_csv_content_type(db, user_and_event):
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    bad_file = UploadFile(
        filename="not-a-csv.png", file=io.BytesIO(b"not csv"),
        headers={"content-type": "image/png"},
    )
    with pytest.raises(HTTPException) as exc:
        upload_applicants(ev.id, _bg_tasks(), bad_file, db, user)
    assert exc.value.status_code == 400


def test_upload_accepts_csv_extension_even_with_octet_stream(db, user_and_event):
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    csv = "Name,Email\nMaya,m@x.com\n"
    f = UploadFile(
        filename="applicants.csv", file=io.BytesIO(csv.encode()),
        headers={"content-type": "application/octet-stream"},
    )
    result = upload_applicants(ev.id, _bg_tasks(), f, db, user)
    assert result.inserted == 1


def test_upload_with_zero_rows_does_not_start_evaluation(db, user_and_event):
    """Empty CSV : don't kick off a useless background scoring run."""
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    result = upload_applicants(ev.id, _bg_tasks(),
                               _upload_file("Name,Email\n"), db, user)
    assert result.inserted == 0
    assert result.evaluation_started is False


# ── list ───────────────────────────────────────────────────────────────

def test_list_applicants_returns_sorted_by_fit_score_when_evaluated(db, user_and_event):
    """When evaluations exist, accepts surface first. Without evaluations,
    falls back to created_at order so newly-uploaded CSVs render predictably."""
    from backend.routes.triage import upload_applicants, list_applicants
    from backend import models
    user, ev = user_and_event
    csv = "Name,Email\nAlpha,a@x.com\nBeta,b@x.com\nGamma,c@x.com\n"
    upload_applicants(ev.id, _bg_tasks(), _upload_file(csv), db, user)
    # No evaluations yet : falls back to created_at order (== CSV order).
    listed = list_applicants(ev.id, db, user)
    assert [a.name for a in listed] == ["Alpha", "Beta", "Gamma"]

    # Now add evaluations with different fit scores. Order should flip.
    by_name = {a.name: a for a in ev.applicants}
    db.add(models.ApplicantEvaluation(
        applicant_id=by_name["Beta"].id, event_id=ev.id,
        fit_score=90, recommendation="accept",
    ))
    db.add(models.ApplicantEvaluation(
        applicant_id=by_name["Gamma"].id, event_id=ev.id,
        fit_score=70, recommendation="maybe",
    ))
    db.add(models.ApplicantEvaluation(
        applicant_id=by_name["Alpha"].id, event_id=ev.id,
        fit_score=30, recommendation="reject",
    ))
    db.commit()
    listed = list_applicants(ev.id, db, user)
    assert [a.name for a in listed] == ["Beta", "Gamma", "Alpha"]
    # And the evaluation field is populated
    assert listed[0].evaluation is not None
    assert listed[0].evaluation.fit_score == 90
    assert listed[0].evaluation.recommendation == "accept"


# ── progress polling ───────────────────────────────────────────────────

def test_evaluation_progress_reports_pending_count(db, user_and_event):
    from backend.routes.triage import (
        upload_applicants, get_evaluation_progress,
    )
    from backend import models
    user, ev = user_and_event
    csv = "Name,Email\nA,1@x.com\nB,2@x.com\nC,3@x.com\n"
    upload_applicants(ev.id, _bg_tasks(), _upload_file(csv), db, user)

    progress = get_evaluation_progress(ev.id, db, user)
    assert progress.total_applicants == 3
    assert progress.scored == 0
    assert progress.pending == 3

    # Score one : pending should go down.
    a = ev.applicants[0]
    db.add(models.ApplicantEvaluation(
        applicant_id=a.id, event_id=ev.id, fit_score=80, recommendation="accept",
    ))
    db.commit()
    progress = get_evaluation_progress(ev.id, db, user)
    assert progress.scored == 1
    assert progress.pending == 2


def test_export_csv_eager_loads_and_is_bounded(db, user_and_event):
    """The CSV export must NOT fire a lazy 2N+1 (evaluation + decision per row)
    -- on a big Luma export that N+1 is slow enough to 524 a plain download. We
    count the SQL statements the export issues and assert it stays flat as rows
    grow (eager selectinload), rather than scaling with the applicant count."""
    from sqlalchemy import event as sa_event
    from backend.routes.triage import export_decisions_csv
    from backend import models
    user, ev = user_and_event

    for i in range(12):
        a = models.Applicant(event_id=ev.id, name=f"P{i}", email=f"p{i}@x.com")
        db.add(a); db.flush()
        db.add(models.ApplicantEvaluation(
            applicant_id=a.id, event_id=ev.id, fit_score=i, recommendation="maybe"))
        db.add(models.ReviewDecision(
            applicant_id=a.id, event_id=ev.id, human_decision="accept"))
    db.commit()
    db.expire_all()   # force real loads, not identity-map hits

    statements: list = []

    def _count(conn, cursor, stmt, params, context, executemany):
        statements.append(stmt)

    sa_event.listen(db.get_bind(), "before_cursor_execute", _count)
    try:
        resp = export_decisions_csv(ev.id, db, user)
    finally:
        sa_event.remove(db.get_bind(), "before_cursor_execute", _count)

    # Flat query count: applicants + evaluation-batch + decision-batch. Nowhere
    # near the ~1 + 2*12 the lazy path would have fired.
    assert len(statements) <= 6, statements

    # Content sanity: all 12 rows present in the streamed CSV. StreamingResponse
    # was built from a single-chunk iterator, so drain it directly.
    import asyncio

    async def _drain():
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(out)

    csv_text = asyncio.new_event_loop().run_until_complete(_drain())
    assert csv_text.count("\n") >= 13   # header + 12 rows
    assert "P0" in csv_text and "P11" in csv_text
