"""
routes/triage.py : Applicant Triage HTTP surface.

Endpoints:
  POST  /events/{id}/triage/config       : set / update triage_config JSON
  GET   /events/{id}/triage/config       : current config (or empty)
  POST  /events/{id}/triage/upload       : multipart CSV, parses + persists
                                           Applicants AND fires background
                                           evaluation (rubric synth + per-
                                           applicant scoring via Exa + Haiku)
  GET   /events/{id}/triage/applicants   : list applicants w/ evaluations
  GET   /events/{id}/triage/evaluations  : poll batch evaluation progress

All routes auth-gated via current_user + scoped via get_owned_event so
users only touch their own events' triage data.
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user, get_owned_event
from ..db import SessionLocal, get_db
from ..triage.csv_parser import parse_csv_file
from ..triage.luma import LumaEvent, LumaFetchError, fetch_luma_event
from ..triage.rubric import synthesize_rubric
from ..triage.score import evaluate_all


router = APIRouter(prefix="/events", tags=["06 · triage"])


# ── Schemas ─────────────────────────────────────────────────────────────

class TriageConfig(BaseModel):
    """Operator-defined sponsor + event criteria. All fields optional so the
    operator can fill the form incrementally without 400-ing the API.

    These flow into the scoring rubric (PR C) which generates per-event
    weighted axes; nothing here is interpreted directly by the scorer."""
    event_type: Optional[str] = None
    sponsor_name: Optional[str] = None
    event_goal: Optional[str] = None
    ideal_attendee_profile: Optional[str] = None
    hard_filters: list[str] = []
    nice_to_have_signals: list[str] = []
    anti_fit_examples: list[str] = []
    capacity: Optional[int] = None
    notes: Optional[str] = None


class EvaluationOut(BaseModel):
    """Applicant evaluation surfaced in /applicants and /applicant/{id}."""
    # `model_version` collides with pydantic's protected `model_` namespace;
    # this opts out so we can keep the natural field name.
    model_config = {"protected_namespaces": ()}

    fit_score: int
    confidence_score: int
    recommendation: str   # accept | maybe | reject | needs_review
    archetype: str
    sponsor_fit: int
    event_fit: int
    role_relevance: int
    company_relevance: int
    stage_relevance: int
    seriousness_legitimacy: int
    room_value: int
    application_quality: int
    one_sentence_summary: str
    why_fit: str
    why_not_fit: str
    evidence_used: list[str]
    missing_info: list[str]
    suggested_review_action: str
    model_version: str


class ApplicantOut(BaseModel):
    """One applicant row as returned by GET /applicants."""
    id: int
    name: str
    email: Optional[str]
    role: Optional[str]
    company: Optional[str]
    website: Optional[str]
    linkedin_url: Optional[str]
    raw_application_data: dict
    evaluation: Optional[EvaluationOut]
    created_at: datetime


class UploadResult(BaseModel):
    event_id: int
    parsed: int       # how many rows the CSV had after the empty-row filter
    inserted: int     # how many we actually wrote (excludes duplicates etc.)
    evaluation_started: bool   # whether bg scoring was kicked off
    applicants: list[ApplicantOut]


class EvaluationProgress(BaseModel):
    """Snapshot of /evaluations polling."""
    event_id: int
    total_applicants: int
    scored: int
    pending: int
    failed: int


# ── Endpoints ──────────────────────────────────────────────────────────


class LumaPreviewBody(BaseModel):
    url: str


@router.post("/triage/luma-preview", response_model=LumaEvent)
def preview_luma_event(
    body: LumaPreviewBody,
    user: models.User = Depends(current_user),
):
    """Fetch a public Luma event page and return parsed metadata so the
    Configure form can auto-fill name / description / capacity / location.

    Auth-gated (current_user) so anonymous traffic can't use us as a free
    proxy. URL validated server-side to lu.ma / luma.com only — see
    triage.luma._validate_luma_url for SSRF hardening."""
    try:
        return fetch_luma_event(body.url)
    except LumaFetchError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{event_id}/triage/config", response_model=TriageConfig)
def set_triage_config(
    event_id: int,
    body: TriageConfig,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Set the sponsor / event criteria that the scoring rubric will key off.
    Idempotent : POSTing again replaces the whole config."""
    ev = get_owned_event(event_id, user, db)
    ev.triage_config = body.model_dump_json()
    db.commit()
    return body


@router.get("/{event_id}/triage/config", response_model=TriageConfig)
def get_triage_config(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    if not ev.triage_config:
        return TriageConfig()
    try:
        return TriageConfig(**json.loads(ev.triage_config))
    except (json.JSONDecodeError, ValueError):
        # Bad JSON shouldn't 500 the UI; return empty config and let the
        # operator re-save.
        return TriageConfig()


@router.post("/{event_id}/triage/upload", response_model=UploadResult)
def upload_applicants(
    event_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Upload a Luma CSV. Parses with flexible field mapping, persists each
    row as an Applicant, and auto-fires background scoring (rubric synth
    via Sonnet + per-applicant scoring via Haiku, with Exa enrichment).

    The endpoint returns IMMEDIATELY after persisting applicants; scoring
    runs in the background. Poll /triage/evaluations for progress.
    """
    ev = get_owned_event(event_id, user, db)
    if not (file.content_type or "").lower().startswith(
        ("text/csv", "application/csv", "application/vnd.ms-excel", "text/plain")
    ) and not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, f"expected a CSV file, got {file.content_type!r}")

    try:
        parsed_rows = parse_csv_file(file.file)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not parse CSV: {type(exc).__name__}: {exc}")

    now = datetime.now(timezone.utc)
    new_applicants: list[models.Applicant] = []
    for row in parsed_rows:
        a = models.Applicant(
            event_id=ev.id,
            name=row.get("name") or "",
            email=row.get("email") or None,
            role=row.get("role") or None,
            company=row.get("company") or None,
            website=row.get("website") or None,
            linkedin_url=row.get("linkedin_url") or None,
            raw_application_data=json.dumps(row.get("raw_application_data") or {}),
            created_at=now,
            updated_at=now,
        )
        db.add(a)
        new_applicants.append(a)
    db.commit()
    for a in new_applicants:
        db.refresh(a)

    started = False
    if new_applicants:
        # Fire-and-forget background scoring : the route returns immediately
        # so the UI can show 'evaluating...' while it runs.
        background_tasks.add_task(_evaluate_event_async, ev.id)
        started = True

    return UploadResult(
        event_id=ev.id,
        parsed=len(parsed_rows),
        inserted=len(new_applicants),
        evaluation_started=started,
        applicants=[_applicant_out(a) for a in new_applicants],
    )


async def _evaluate_event_async(event_id: int) -> None:
    """Background-task body : run rubric synth + per-applicant scoring on
    its own SessionLocal session so we don't tie up the request-scoped db.

    Best-effort : exceptions are swallowed + logged so a failing eval can't
    crash the request that scheduled it.
    """
    bg_db = SessionLocal()
    try:
        ev = bg_db.get(models.Event, event_id)
        if ev is None:
            return
        applicants = list(ev.applicants)
        if not applicants:
            return
        rubric = synthesize_rubric(ev.id, ev.triage_config or "", applicants)
        await evaluate_all(bg_db, ev, rubric)
    except Exception as exc:  # noqa: BLE001
        print(f"  [triage.evaluate_event_async] {event_id}: "
              f"{type(exc).__name__}: {exc}")
    finally:
        bg_db.close()


@router.get("/{event_id}/triage/applicants", response_model=list[ApplicantOut])
def list_applicants(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """All applicants for this event. Sorted by fit_score descending when
    evaluations are present (so the review queue surfaces accepts first),
    falls back to created_at ascending."""
    ev = get_owned_event(event_id, user, db)
    rows = list(ev.applicants)
    rows.sort(key=lambda a: (
        -(a.evaluation.fit_score if a.evaluation else -1),
        a.created_at,
    ))
    return [_applicant_out(a) for a in rows]


@router.get("/{event_id}/triage/evaluations", response_model=EvaluationProgress)
def get_evaluation_progress(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Poll endpoint : how many applicants have been scored vs. still pending.
    The UI calls this on a timer after upload to show progress."""
    ev = get_owned_event(event_id, user, db)
    total = len(ev.applicants)
    scored = sum(1 for a in ev.applicants if a.evaluation is not None)
    return EvaluationProgress(
        event_id=ev.id,
        total_applicants=total,
        scored=scored,
        pending=max(0, total - scored),
        failed=0,  # TODO: track failures explicitly on the evaluation row
    )


def _applicant_out(a: models.Applicant) -> ApplicantOut:
    try:
        raw = json.loads(a.raw_application_data or "{}")
        if not isinstance(raw, dict):
            raw = {}
    except json.JSONDecodeError:
        raw = {}

    evaluation: Optional[EvaluationOut] = None
    if a.evaluation is not None:
        ev = a.evaluation
        try:
            evidence = json.loads(ev.evidence_used or "[]")
            if not isinstance(evidence, list):
                evidence = []
        except json.JSONDecodeError:
            evidence = []
        try:
            missing = json.loads(ev.missing_info or "[]")
            if not isinstance(missing, list):
                missing = []
        except json.JSONDecodeError:
            missing = []
        evaluation = EvaluationOut(
            fit_score=ev.fit_score, confidence_score=ev.confidence_score,
            recommendation=ev.recommendation, archetype=ev.archetype,
            sponsor_fit=ev.sponsor_fit, event_fit=ev.event_fit,
            role_relevance=ev.role_relevance, company_relevance=ev.company_relevance,
            stage_relevance=ev.stage_relevance,
            seriousness_legitimacy=ev.seriousness_legitimacy,
            room_value=ev.room_value, application_quality=ev.application_quality,
            one_sentence_summary=ev.one_sentence_summary,
            why_fit=ev.why_fit, why_not_fit=ev.why_not_fit,
            evidence_used=[str(x) for x in evidence],
            missing_info=[str(x) for x in missing],
            suggested_review_action=ev.suggested_review_action,
            model_version=ev.model_version,
        )

    return ApplicantOut(
        id=a.id, name=a.name, email=a.email, role=a.role, company=a.company,
        website=a.website, linkedin_url=a.linkedin_url,
        raw_application_data=raw,
        evaluation=evaluation,
        created_at=a.created_at,
    )
