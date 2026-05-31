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

import csv
import io

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..auth import current_user, get_owned_event
from ..db import SessionLocal, get_db
from ..triage.csv_parser import parse_csv_file
from ..triage.luma import (
    LumaEvent, LumaFetchError, TriageSuggestion,
    fetch_luma_event, suggest_triage_config,
)
from ..triage.rubric import synthesize_rubric, icp_from_event
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
    # UI-only round-trip for Triage "Configure" when it mirrors outbound Intake.
    # Stripped before the rubric LLM sees the config (see triage.rubric).
    intake_snapshot: Optional[dict] = None


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
    # Judge B (evidence auditor) outcome. verifier_ran is False for the clean
    # majority (audit gated to risky applicants). When it ran, adjustments lists
    # exactly what the deterministic consolidator changed (confidence caps,
    # blocked accepts, forced reviews) and verifier_reason is the audit summary.
    verifier_ran: bool = False
    verifier_adjustments: list[str] = []
    verifier_reason: str = ""


class DecisionOut(BaseModel):
    """Operator's accept/maybe/reject decision on an applicant, if any."""
    human_decision: str
    reviewer_notes: str
    system_recommendation: str
    reviewed_at: datetime


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
    decision: Optional[DecisionOut]
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


class LumaPreviewResponse(BaseModel):
    """Combined : the parsed page + Claude-inferred suggestions for fields
    the page doesn't carry (sponsor, ideal-attendee, anti-fit, etc).

    Frontend should use `event` to fill name/description/capacity directly,
    and use `suggestions` to pre-fill the operator-judgment fields — with
    the expectation that the operator reviews + tightens before saving."""
    event: LumaEvent
    suggestions: TriageSuggestion


@router.post("/triage/luma-preview", response_model=LumaPreviewResponse)
def preview_luma_event(
    body: LumaPreviewBody,
    user: models.User = Depends(current_user),
):
    """Fetch a public event page (Luma / Partiful) and return parsed
    metadata + Claude-inferred triage suggestions so the Configure form
    can auto-fill.

    Auth-gated (current_user) so anonymous traffic can't use us as a free
    proxy. URL validated server-side to the supported event hosts only —
    see triage.luma._validate_luma_url for SSRF hardening. Suggestion call
    is best-effort : on Anthropic failure we still return the parsed event
    with empty suggestions instead of 500-ing."""
    try:
        event = fetch_luma_event(body.url)
    except LumaFetchError as exc:
        raise HTTPException(400, str(exc))
    suggestions = suggest_triage_config(event)
    return LumaPreviewResponse(event=event, suggestions=suggestions)


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
            name=_clip(row.get("name"), "name") or "",
            email=_clip(row.get("email"), "email") or None,
            role=_clip(row.get("role"), "role") or None,
            company=_clip(row.get("company"), "company") or None,
            website=_clip(row.get("website"), "website") or None,
            linkedin_url=_clip(row.get("linkedin_url"), "linkedin_url") or None,
            # Unmapped CSV columns (the long free-text survey answers) are
            # retained verbatim here in an unbounded TEXT column — only the
            # mapped, length-capped identifier fields above get clipped.
            raw_application_data=json.dumps(row.get("raw_application_data") or {}),
            created_at=now,
            updated_at=now,
        )
        db.add(a)
        new_applicants.append(a)
    inserted = len(new_applicants)
    db.commit()

    started = False
    if new_applicants:
        # Fire-and-forget background scoring : the route returns immediately
        # so the UI can show 'evaluating...' while it runs.
        background_tasks.add_task(_evaluate_event_async, ev.id)
        started = True

    # NOTE: we deliberately do NOT serialize the inserted applicants back here.
    # A real Luma export is 500+ rows; expire_on_commit means each one would
    # need a refresh + lazy-load of its (still-empty) evaluation/decision, i.e.
    # ~3 queries/row. At 530 rows that blew past Cloudflare's 100s edge timeout
    # and returned a 524 — even though the rows committed fine, so every retry
    # silently duplicated the whole event. The UI only reads `parsed`/`inserted`
    # and then polls the list endpoint, so the per-row payload was pure waste.
    return UploadResult(
        event_id=ev.id,
        parsed=len(parsed_rows),
        inserted=inserted,
        evaluation_started=started,
        applicants=[],
    )


async def _evaluate_event_async(event_id: int, *,
                                force_reenrich: bool = False) -> None:
    """Background-task body : run rubric synth + per-applicant scoring on
    its own SessionLocal session so we don't tie up the request-scoped db.

    Best-effort : exceptions are swallowed + logged so a failing eval can't
    crash the request that scheduled it.
    """
    # Loud progress prints so a silent failure (bg task never fired ; rubric
    # synth returned a default ; no applicants) is visible in deploy logs.
    # Cheap to leave on : two lines per upload, no PII.
    print(f"  [triage.eval] start event={event_id}")
    bg_db = SessionLocal()
    try:
        ev = bg_db.get(models.Event, event_id)
        if ev is None:
            print(f"  [triage.eval] event={event_id} NOT FOUND in bg session")
            return
        applicants = list(ev.applicants)
        if not applicants:
            print(f"  [triage.eval] event={event_id} has 0 applicants ; nothing to score")
            return
        print(f"  [triage.eval] event={event_id} scoring {len(applicants)} applicants")
        # Anchor the inbound rubric to the operator's ICP from event setup
        # (role / seniority / co_stage / format / city / yoe). This is the
        # ICP → triage hook: the same profile the outbound curation path scores
        # against now seeds the inbound rubric synthesis.
        rubric = synthesize_rubric(
            ev.id, ev.triage_config or "", applicants,
            icp=icp_from_event(ev),
        )
        await evaluate_all(bg_db, ev, rubric, force_reenrich=force_reenrich)
        print(f"  [triage.eval] event={event_id} done")
    except Exception as exc:  # noqa: BLE001
        print(f"  [triage.evaluate_event_async] {event_id}: "
              f"{type(exc).__name__}: {exc}")
    finally:
        bg_db.close()


@router.post("/{event_id}/triage/re-evaluate")
def re_evaluate(
    event_id: int,
    background_tasks: BackgroundTasks,
    reenrich: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Re-run rubric synth + per-applicant scoring for this event. Useful
    when the first pass failed (e.g. a model API hiccup) and the operator
    wants to retry without re-uploading the CSV.

    By default this REUSES each applicant's frozen raw enrichment, so re-running
    after a triage_config / ICP edit re-scores the SAME evidence deterministically
    (only the rubric changes). Pass ?reenrich=true to also refresh the underlying
    Unipile/Exa evidence — e.g. when an applicant updated their LinkedIn.

    Clears the rubric cache so a stale 'default rubric' from a failed run
    doesn't get reused."""
    ev = get_owned_event(event_id, user, db)
    from ..triage.rubric import _RUBRIC_CACHE
    _RUBRIC_CACHE.clear()
    background_tasks.add_task(_evaluate_event_async, ev.id,
                             force_reenrich=reenrich)
    return {"event_id": ev.id, "re_evaluation_started": True,
            "reenrich": reenrich,
            "applicant_count": len(ev.applicants)}


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
    # Eager-load evaluation + decision in a couple of IN-queries instead of
    # lazy-loading per row. A real Luma event is 500+ applicants; the old
    # `list(ev.applicants)` + per-row `a.evaluation`/`a.decision` access fired
    # ~2 queries/row (~1k+ total), holding a pooled connection for seconds.
    # The UI polls this on a timer while background scoring also holds a
    # connection, so under contention the pool (size 5 + overflow 5) drained
    # and requests 500'd with a QueuePool timeout. selectinload makes it O(1).
    rows = (
        db.query(models.Applicant)
        .options(
            selectinload(models.Applicant.evaluation),
            selectinload(models.Applicant.decision),
        )
        .filter(models.Applicant.event_id == ev.id)
        .all()
    )
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
    # Two COUNT queries instead of loading every applicant + lazy-loading its
    # evaluation. This endpoint is polled on a timer, so it must stay cheap —
    # the old per-row version drained the connection pool on 500+ row events.
    total = db.query(func.count(models.Applicant.id)).filter(
        models.Applicant.event_id == ev.id
    ).scalar() or 0
    scored = db.query(func.count(models.ApplicantEvaluation.id)).filter(
        models.ApplicantEvaluation.event_id == ev.id
    ).scalar() or 0
    return EvaluationProgress(
        event_id=ev.id,
        total_applicants=total,
        scored=scored,
        pending=max(0, total - scored),
        failed=0,  # TODO: track failures explicitly on the evaluation row
    )


_VALID_DECISIONS = {"accept", "maybe", "reject", "needs_review"}


class DecisionBody(BaseModel):
    decision: str
    notes: Optional[str] = ""


@router.post(
    "/{event_id}/triage/applicants/{applicant_id}/decision",
    response_model=ApplicantOut,
)
def set_decision(
    event_id: int,
    applicant_id: int,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Upsert the operator's accept/maybe/reject decision for an applicant.
    Records the system_recommendation snapshot at decision time so we can
    measure override rate later."""
    ev = get_owned_event(event_id, user, db)
    decision = (body.decision or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        raise HTTPException(
            400, f"decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}",
        )
    applicant = db.get(models.Applicant, applicant_id)
    if applicant is None or applicant.event_id != ev.id:
        raise HTTPException(404, "applicant not found on this event")

    system_rec = applicant.evaluation.recommendation if applicant.evaluation else ""
    now = datetime.now(timezone.utc)
    if applicant.decision is None:
        applicant.decision = models.ReviewDecision(
            applicant_id=applicant.id,
            event_id=ev.id,
            system_recommendation=system_rec,
            human_decision=decision,
            reviewer_notes=(body.notes or "").strip(),
            reviewed_at=now,
        )
        db.add(applicant.decision)
    else:
        applicant.decision.human_decision = decision
        applicant.decision.reviewer_notes = (body.notes or "").strip()
        applicant.decision.system_recommendation = system_rec
        applicant.decision.reviewed_at = now
    db.commit()
    db.refresh(applicant)
    return _applicant_out(applicant)


@router.get("/{event_id}/triage/export.csv")
def export_decisions_csv(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Stream a CSV of all applicants with AI scores + operator decisions,
    suitable for re-importing into Luma (or just sharing the cut list with
    the sponsor). Includes both raw applicant fields and the audit trail :
    system_recommendation, human_decision, reviewer_notes, reviewed_at."""
    ev = get_owned_event(event_id, user, db)
    rows = sorted(
        ev.applicants,
        key=lambda a: (
            -(a.evaluation.fit_score if a.evaluation else -1),
            a.created_at,
        ),
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "applicant_id", "name", "email", "role", "company",
        "linkedin_url", "website",
        "fit_score", "confidence_score", "system_recommendation", "archetype",
        "one_sentence_summary",
        "verifier_ran", "verifier_reason",
        "human_decision", "reviewer_notes", "reviewed_at",
    ])
    for a in rows:
        e = a.evaluation
        d = a.decision
        writer.writerow([
            a.id, a.name, a.email or "", a.role or "", a.company or "",
            a.linkedin_url or "", a.website or "",
            (e.fit_score if e else ""),
            (e.confidence_score if e else ""),
            (e.recommendation if e else ""),
            (e.archetype if e else ""),
            (e.one_sentence_summary if e else ""),
            (bool(getattr(e, "verifier_ran", False)) if e else ""),
            (getattr(e, "verifier_reason", "") or "" if e else ""),
            (d.human_decision if d else ""),
            (d.reviewer_notes if d else ""),
            (d.reviewed_at.isoformat() if d else ""),
        ])
    buf.seek(0)
    filename = f"triage-event-{ev.id}-decisions.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


# Max lengths for the indexed Applicant string columns, read straight from the
# model so they can't drift if a column is resized. Luma exports map free-text
# survey answers ("what do you do") into role/company, which can blow past the
# VARCHAR(200) cap — Postgres then rejects the INSERT with a hard 500 while
# SQLite (local/tests) silently accepts it. We clip these mapped columns on
# write; unmapped CSV columns are kept verbatim in raw_application_data (TEXT).
_APPLICANT_COL_MAXLEN: dict[str, int] = {
    c.name: c.type.length
    for c in models.Applicant.__table__.columns
    if getattr(c.type, "length", None)
}


def _clip(value: Optional[str], field: str) -> Optional[str]:
    """Trim a string to its column's max length (no-op if it already fits or
    the value is None)."""
    if value is None:
        return None
    maxlen = _APPLICANT_COL_MAXLEN.get(field)
    if maxlen is not None and len(value) > maxlen:
        return value[:maxlen]
    return value


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
        try:
            adjustments = json.loads(getattr(ev, "verifier_adjustments", None) or "[]")
            if not isinstance(adjustments, list):
                adjustments = []
        except json.JSONDecodeError:
            adjustments = []
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
            verifier_ran=bool(getattr(ev, "verifier_ran", False)),
            verifier_adjustments=[str(x) for x in adjustments],
            verifier_reason=getattr(ev, "verifier_reason", "") or "",
        )

    decision: Optional[DecisionOut] = None
    if a.decision is not None:
        d = a.decision
        decision = DecisionOut(
            human_decision=d.human_decision,
            reviewer_notes=d.reviewer_notes or "",
            system_recommendation=d.system_recommendation or "",
            reviewed_at=d.reviewed_at,
        )

    return ApplicantOut(
        id=a.id, name=a.name, email=a.email, role=a.role, company=a.company,
        website=a.website, linkedin_url=a.linkedin_url,
        raw_application_data=raw,
        evaluation=evaluation,
        decision=decision,
        created_at=a.created_at,
    )
