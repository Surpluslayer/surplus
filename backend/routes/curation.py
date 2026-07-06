"""
routes/curation.py : the HTTP surface for the audience-ingest workflow.

Five-stage map, mirroring backend/curation/:

  Stage 1 (Ingest)
    POST   /events/{id}/curation/attendees/preview-mapping  CSV column guess
    POST   /events/{id}/curation/attendees/import           commit CSV
    GET    /events/{id}/curation/attendees                  list with scores
    POST   /events/{id}/curation/attendees/{aid}/enrich     Claude enrichment
    POST   /events/{id}/curation/attendees/enrich-all       bulk enrichment

  Stage 2 (Curate & score)
    POST   /events/{id}/curation/icp                        operator ICP override
    POST   /events/{id}/curation/score                      rule-based + Claude rationale
    GET    /events/{id}/curation/high-fit                   ranked above-threshold list
    POST   /events/{id}/curation/gap-analysis               distribution delta

  Stage 3 (Match)
    POST   /events/{id}/curation/intros/build               rebuild intro recs
    GET    /events/{id}/curation/attendees/{aid}/intros     pre-event intro card

  Stage 4 (Activate)
    POST   /events/{id}/curation/attendees/{aid}/outreach   personalized compose

  Stage 5 (ROI)
    POST   /events/{id}/curation/attendees/{aid}/follow-up  log follow-up
    GET    /events/{id}/curation/attendees/{aid}/follow-ups list follow-ups
    POST   /events/{id}/curation/attendees/{aid}/attribute  Claude attribution
    GET    /events/{id}/curation/attendees/{aid}/llm-log    audit trail
    GET    /events/{id}/curation/features                   feature-flag snapshot

  NEAR-TERM (gated by SURPLUS_FEATURE_* env vars)
    POST   /events/{id}/curation/near-term/news-signal/{aid}
    POST   /events/{id}/curation/near-term/recognition
    POST   /events/{id}/curation/near-term/warm-connection/{aid}
    POST   /events/{id}/curation/near-term/predict-no-show
    POST   /events/{id}/curation/near-term/sponsor-match
    POST   /events/{id}/curation/near-term/seating
    POST   /events/{id}/curation/near-term/session-relevance
    GET    /events/{id}/curation/near-term/sponsor-roi
    GET    /events/{id}/curation/near-term/news-attribution/{aid}
    GET    /events/{id}/curation/near-term/recurring-memory

All endpoints require auth + ownership via the existing get_owned_event.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user, get_owned_event
from ..db import get_db
from ..jobs import run_detached
from ..triage.csv_parser import MAX_UPLOAD_BYTES, enforce_upload_size
from ..curation import (
    attribution as attribution_mod,
    csv_import,
    enrichment as enrichment_mod,
    features,
    gap_analysis,
    intros,
    near_term,
    outreach as outreach_mod,
    scoring,
)


router = APIRouter(prefix="/events", tags=["07 · curation"])


# ── Schemas ────────────────────────────────────────────────────────────

class AttendeeOut(BaseModel):
    id: int
    name: str
    email: Optional[str]
    role: Optional[str]
    company: Optional[str]
    seniority: Optional[str]
    linkedin_url: Optional[str]
    list_source: str
    rsvp_status: Optional[str]
    fit_score: int
    fit_rule_trace: list[str]
    fit_rationale: str
    enrichment: dict
    raw: dict
    no_show_probability: Optional[float] = None
    no_show_rationale: str = ""
    recognition_flags: list[str] = []
    has_warm_connection: bool = False
    news_signal_count: int = 0
    created_at: datetime

    @classmethod
    def of(cls, a: models.Attendee) -> "AttendeeOut":
        def _safe_list(s: str) -> list:
            try:
                v = json.loads(s or "[]")
            except json.JSONDecodeError:
                return []
            return v if isinstance(v, list) else []

        def _safe_dict(s: str) -> dict:
            try:
                v = json.loads(s or "{}")
            except json.JSONDecodeError:
                return {}
            return v if isinstance(v, dict) else {}

        news_payload = _safe_dict(a.news_signal)
        return cls(
            id=a.id, name=a.name, email=a.email, role=a.role,
            company=a.company, seniority=a.seniority,
            linkedin_url=a.linkedin_url, list_source=a.list_source,
            rsvp_status=a.rsvp_status,
            fit_score=a.fit_score,
            fit_rule_trace=_safe_list(a.fit_rule_trace),
            fit_rationale=a.fit_rationale or "",
            enrichment=enrichment_mod.get_enrichment(a),
            raw=_safe_dict(a.raw),
            no_show_probability=a.no_show_probability,
            no_show_rationale=a.no_show_rationale or "",
            recognition_flags=_safe_list(a.recognition_flags),
            has_warm_connection=bool(_safe_dict(a.warm_connection)),
            news_signal_count=len((news_payload.get("signals") or [])),
            created_at=a.created_at,
        )


class PreviewMappingResult(BaseModel):
    columns: list[str]
    mapping: dict[str, str]
    sample: list[dict]
    row_count: int


class ImportBody(BaseModel):
    # Bound the inline CSV so a giant JSON body can't OOM the single worker
    # (defense-in-depth alongside the edge/proxy body limit). ~1 byte/char.
    csv: str = Field(max_length=MAX_UPLOAD_BYTES)
    mapping: dict[str, str] = Field(default_factory=dict)
    list_source: str = "other"
    default_rsvp: Optional[str] = None


class ImportResult(BaseModel):
    event_id: int
    inserted: int
    skipped_duplicates_or_empty: int
    applied_mapping: dict[str, str]
    attendees: list[AttendeeOut]


class ICPBody(BaseModel):
    role: str = ""
    seniority: str = ""
    function: str = ""
    company_stage: str = ""
    company_industry: str = ""
    company_size_bucket: str = ""
    keywords: list[str] = []


class ScoreResult(BaseModel):
    event_id: int
    scored: int
    above_threshold: int
    threshold: int
    method: str = "rule_based"
    attendees: list[AttendeeOut]


class GapAnalysisBody(BaseModel):
    target_distributions: dict[str, dict[str, float]]
    headcount: Optional[int] = None


class FollowUpBody(BaseModel):
    kind: str = "other"
    notes: str = ""
    occurred_at: Optional[datetime] = None


class FollowUpOut(BaseModel):
    id: int
    attendee_id: int
    kind: str
    notes: str
    occurred_at: Optional[datetime]
    created_at: datetime


class AttributionBody(BaseModel):
    operator_notes: str = ""


class AttributionOut(BaseModel):
    id: int
    attendee_id: int
    event_id: int
    outcome: str
    confidence: float
    value: int
    rationale: str
    evidence: list[str]
    created_at: datetime

    @classmethod
    def of(cls, row: models.AttendeeAttribution) -> "AttributionOut":
        try:
            ev = json.loads(row.evidence or "[]")
        except json.JSONDecodeError:
            ev = []
        return cls(
            id=row.id, attendee_id=row.attendee_id, event_id=row.event_id,
            outcome=row.outcome, confidence=row.confidence, value=row.value,
            rationale=row.rationale,
            evidence=[str(e) for e in (ev if isinstance(ev, list) else [])],
            created_at=row.created_at,
        )


class LLMCallOut(BaseModel):
    id: int
    purpose: str
    model: str
    status: str
    prompt: str
    output: str
    error: Optional[str]
    latency_ms: Optional[int]
    created_at: datetime


# ── Helpers ────────────────────────────────────────────────────────────

def _icp_for_event(event: models.Event, db: Session) -> scoring.ICP:
    """ICP resolution order: event-stored override (in triage_config JSON
    blob, under the `curation_icp` key) -> derived-from-Event-intake."""
    raw = (event.triage_config or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            stored = payload.get("curation_icp") if isinstance(payload, dict) else None
            if isinstance(stored, dict):
                return scoring.ICP(
                    role=stored.get("role", ""),
                    seniority=stored.get("seniority", ""),
                    function=stored.get("function", ""),
                    company_stage=stored.get("company_stage", ""),
                    company_industry=stored.get("company_industry", ""),
                    company_size_bucket=stored.get("company_size_bucket", ""),
                    keywords=stored.get("keywords") or [],
                )
        except json.JSONDecodeError:
            pass
    return scoring.ICP.from_event(event)


def _get_attendee(db: Session, event: models.Event,
                   attendee_id: int) -> models.Attendee:
    a = db.get(models.Attendee, attendee_id)
    if a is None or a.event_id != event.id:
        raise HTTPException(404, "attendee not found on this event")
    return a


# ── Stage 1: Ingest ────────────────────────────────────────────────────

@router.post("/{event_id}/curation/attendees/preview-mapping",
              response_model=PreviewMappingResult)
async def preview_mapping(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Step 1 of CSV import : parse the file, guess a column mapping,
    return the first 5 rows for the UI to render a confirmation table."""
    get_owned_event(event_id, user, db)
    # Bounded read so an oversized upload is rejected (413) instead of buffering
    # unboundedly and OOMing the single web worker.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    enforce_upload_size(content)
    proposal = csv_import.propose_mapping(content)
    return PreviewMappingResult(**proposal)


@router.post("/{event_id}/curation/attendees/import",
              response_model=ImportResult)
def import_attendees(
    event_id: int,
    body: ImportBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Step 2 of CSV import : apply the (possibly operator-edited) mapping,
    dedupe against existing attendees on the event, persist new rows.

    Idempotent : re-importing the same CSV is a no-op (every row dedupes)."""
    ev = get_owned_event(event_id, user, db)
    inserted, skipped, applied = csv_import.import_csv(
        db, ev.id, body.csv.encode("utf-8"),
        mapping=body.mapping,
        list_source=body.list_source,
        default_rsvp=body.default_rsvp,
    )
    db.commit()
    for a in inserted:
        db.refresh(a)
    return ImportResult(
        event_id=ev.id,
        inserted=len(inserted),
        skipped_duplicates_or_empty=skipped,
        applied_mapping=applied,
        attendees=[AttendeeOut.of(a) for a in inserted],
    )


# Hard ceiling on how many attendee rows a single list call materializes +
# serializes. A runaway import (thousands of rows) otherwise loads the whole
# table into memory and builds thousands of pydantic objects in the request
# path, which is slow enough to pin a worker. The UI ranks by fit_score and
# shows the top slice; 2000 is far above any real guest list.
_ATTENDEE_LIST_MAX = 2000


@router.get("/{event_id}/curation/attendees",
            response_model=list[AttendeeOut])
def list_attendees(
    event_id: int,
    list_source: Optional[str] = None,
    rsvp_status: Optional[str] = None,
    limit: int = _ATTENDEE_LIST_MAX,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """All attendees for the event. Optional filters narrow by list_source
    or rsvp_status (handy for "show me only the alumni rows").

    Bounded by `limit` (capped at _ATTENDEE_LIST_MAX) so a runaway import can't
    make this response unboundedly large and pin a worker; rows are fit-ranked
    so the cap keeps the most relevant attendees."""
    ev = get_owned_event(event_id, user, db)
    limit = max(1, min(limit, _ATTENDEE_LIST_MAX))
    q = db.query(models.Attendee).filter(models.Attendee.event_id == ev.id)
    if list_source:
        q = q.filter(models.Attendee.list_source == list_source)
    if rsvp_status:
        q = q.filter(models.Attendee.rsvp_status == rsvp_status)
    rows = q.order_by(models.Attendee.fit_score.desc(),
                      models.Attendee.created_at.asc()).limit(limit).all()
    return [AttendeeOut.of(a) for a in rows]


@router.post("/{event_id}/curation/attendees/{aid}/enrich",
              response_model=AttendeeOut)
def enrich_one(
    event_id: int,
    aid: int,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Run Claude enrichment on one attendee. Cached on the row : pass
    `?refresh=true` to bust the cache and re-call."""
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    enrichment_mod.enrich_attendee(db, a, refresh=refresh)
    db.commit()
    db.refresh(a)
    return AttendeeOut.of(a)


def _run_enrich_all(db: Session, event_id: int, refresh: bool) -> None:
    """Detached body for enrich-all (jobs.run_detached owns `db`). Enriches every
    attendee on the event; each enrich_attendee is a per-row Claude call. Commits
    per row so partial progress survives a crash and the poller sees rows fill in.
    Fail-soft per row: one bad enrichment must not sink the whole batch."""
    rows = db.query(models.Attendee).filter(
        models.Attendee.event_id == event_id
    ).all()
    for a in rows:
        try:
            enrichment_mod.enrich_attendee(db, a, refresh=refresh)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [enrich-all] event={event_id} attendee={a.id} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)


@router.post("/{event_id}/curation/attendees/enrich-all", status_code=202)
def enrich_all(
    event_id: int,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
) -> dict:
    """Enrich every attendee on the event in one batch.

    Each attendee is a Claude call, so a large list is minutes of network work.
    Running that inline held the request open long enough to trip the edge
    timeout (a 524 -- and every retry re-ran the whole batch). It now runs
    DETACHED on its own session (jobs.run_detached) and the route returns a fast
    202 ack; the UI polls GET /curation/attendees to watch rows fill in.
    """
    ev = get_owned_event(event_id, user, db)
    total = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).count()
    run_detached(_run_enrich_all, ev.id, refresh)
    return {"event_id": ev.id, "started": True, "total": total}


# ── Stage 2: Curate & score ────────────────────────────────────────────

@router.post("/{event_id}/curation/icp", response_model=ICPBody)
def set_icp(
    event_id: int,
    body: ICPBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Override the auto-derived ICP. Stored on Event.triage_config JSON
    under the `curation_icp` key so we don't need a new column."""
    ev = get_owned_event(event_id, user, db)
    existing: dict = {}
    if ev.triage_config:
        try:
            parsed = json.loads(ev.triage_config)
            if isinstance(parsed, dict):
                existing = parsed
        except json.JSONDecodeError:
            existing = {}
    existing["curation_icp"] = body.model_dump()
    ev.triage_config = json.dumps(existing)
    db.commit()
    return body


@router.get("/{event_id}/curation/icp", response_model=ICPBody)
def get_icp(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Read back the current ICP (override if set, else derived)."""
    ev = get_owned_event(event_id, user, db)
    icp = _icp_for_event(ev, db)
    return ICPBody(
        role=icp.role, seniority=",".join(icp.seniority),
        function=",".join(icp.function),
        company_stage=",".join(icp.company_stage),
        company_industry=",".join(icp.company_industry),
        company_size_bucket=",".join(icp.company_size_bucket),
        keywords=list(icp.keywords),
    )


@router.post("/{event_id}/curation/score", response_model=ScoreResult)
def score_attendees(
    event_id: int,
    threshold: int = 70,
    with_rationale: bool = True,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Score every attendee. RULE-BASED: deterministic, auditable.

    `with_rationale=true` ALSO writes a Claude-generated one-sentence
    rationale per attendee. The score itself doesn't change either way.
    `threshold` only affects the count in the response : it's not stored.
    """
    ev = get_owned_event(event_id, user, db)
    icp = _icp_for_event(ev, db)
    rows = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    above = 0
    for a in rows:
        score, _trace, _rationale = scoring.score_and_explain(
            db, a, icp, with_rationale=with_rationale,
        )
        a.updated_at = datetime.now(timezone.utc)
        if score >= threshold:
            above += 1
    db.commit()
    for a in rows:
        db.refresh(a)
    return ScoreResult(
        event_id=ev.id, scored=len(rows), above_threshold=above,
        threshold=threshold,
        attendees=sorted([AttendeeOut.of(a) for a in rows],
                          key=lambda x: -x.fit_score),
    )


@router.get("/{event_id}/curation/high-fit",
            response_model=list[AttendeeOut])
def high_fit(
    event_id: int,
    threshold: int = 70,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Top-N attendees by fit_score, filtered to >=threshold."""
    ev = get_owned_event(event_id, user, db)
    rows = (db.query(models.Attendee)
              .filter(models.Attendee.event_id == ev.id,
                      models.Attendee.fit_score >= threshold)
              .order_by(models.Attendee.fit_score.desc())
              .limit(limit).all())
    return [AttendeeOut.of(a) for a in rows]


@router.post("/{event_id}/curation/gap-analysis")
def gap_analysis_endpoint(
    event_id: int,
    body: GapAnalysisBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Compute the delta between the operator's target distribution and the
    current attendee list. Rule-based (labelled in the response)."""
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    return gap_analysis.compute_gap(
        ev, attendees, body.target_distributions,
        headcount_override=body.headcount,
    )


# ── Stage 3: Match ─────────────────────────────────────────────────────

@router.post("/{event_id}/curation/intros/build")
def build_intros(
    event_id: int,
    min_weight: float = intros.MIN_WEIGHT,
    max_per_attendee: int = 6,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Recompute intro recommendations for every attendee on the event.

    Idempotent. Returns aggregate counts; per-attendee cards are read
    separately via /attendees/{aid}/intros.
    """
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    built = intros.build_intros_for_event(
        db, ev.id, attendees,
        min_weight=min_weight, max_per_attendee=max_per_attendee,
    )
    db.commit()
    return {
        "event_id": ev.id,
        "attendees": len(attendees),
        "intros": len(built),
        "min_weight": min_weight,
        "max_per_attendee": max_per_attendee,
        "method": "rule_based",
    }


@router.get("/{event_id}/curation/attendees/{aid}/intros")
def get_intro_card(
    event_id: int,
    aid: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Pre-event intro list for one attendee : the card the operator hands
    them. Method label exposed so the UI can show 'rule-based pairing'."""
    ev = get_owned_event(event_id, user, db)
    _get_attendee(db, ev, aid)
    return intros.export_intro_card(db, ev.id, aid)


# ── Stage 4: Activate ──────────────────────────────────────────────────

@router.post("/{event_id}/curation/attendees/{aid}/outreach")
def compose_outreach(
    event_id: int,
    aid: int,
    slot: str = "high_fit_invite",
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Compose one personalized outreach message for `aid`.

    `slot` ∈ {high_fit_invite, gap_fill, reminder}. Returns the text and
    its origin (llm | template); does NOT send."""
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    if slot not in ("high_fit_invite", "gap_fill", "reminder"):
        raise HTTPException(422, f"unknown slot: {slot}")
    result = outreach_mod.compose_for_attendee(db, a, ev, slot=slot)
    db.commit()
    return {"attendee_id": a.id, "event_id": ev.id, **result}


# ── Stage 5: ROI ───────────────────────────────────────────────────────

@router.post("/{event_id}/curation/attendees/{aid}/follow-up",
              response_model=FollowUpOut, status_code=201)
def log_follow_up(
    event_id: int,
    aid: int,
    body: FollowUpBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Log a post-event follow-up on this attendee."""
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    row = models.AttendeeFollowUp(
        attendee_id=a.id, event_id=ev.id,
        kind=body.kind, notes=body.notes,
        occurred_at=body.occurred_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return FollowUpOut(
        id=row.id, attendee_id=row.attendee_id, kind=row.kind,
        notes=row.notes, occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


@router.get("/{event_id}/curation/attendees/{aid}/follow-ups",
            response_model=list[FollowUpOut])
def list_follow_ups(
    event_id: int,
    aid: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    _get_attendee(db, ev, aid)
    rows = (db.query(models.AttendeeFollowUp)
              .filter(models.AttendeeFollowUp.attendee_id == aid,
                      models.AttendeeFollowUp.event_id == ev.id)
              .order_by(models.AttendeeFollowUp.created_at.asc())
              .all())
    return [FollowUpOut(
        id=r.id, attendee_id=r.attendee_id, kind=r.kind,
        notes=r.notes, occurred_at=r.occurred_at, created_at=r.created_at,
    ) for r in rows]


@router.post("/{event_id}/curation/attendees/{aid}/attribute",
              response_model=AttributionOut)
def run_attribution(
    event_id: int,
    aid: int,
    body: AttributionBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Run Claude outcome attribution for one attendee. Idempotent : prior
    attribution row gets replaced. Audit-logged."""
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    row = attribution_mod.attribute_attendee(
        db, a, ev, operator_notes=body.operator_notes,
    )
    db.commit()
    db.refresh(row)
    return AttributionOut.of(row)


@router.get("/{event_id}/curation/attendees/{aid}/attribution",
            response_model=Optional[AttributionOut])
def get_attribution(
    event_id: int,
    aid: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    _get_attendee(db, ev, aid)
    row = (db.query(models.AttendeeAttribution)
             .filter(models.AttendeeAttribution.attendee_id == aid,
                     models.AttendeeAttribution.event_id == ev.id)
             .order_by(models.AttendeeAttribution.created_at.desc())
             .first())
    return AttributionOut.of(row) if row else None


@router.get("/{event_id}/curation/attendees/{aid}/llm-log",
            response_model=list[LLMCallOut])
def llm_log(
    event_id: int,
    aid: int,
    purpose: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Audit trail : every Claude call made on this attendee. Filter by
    `purpose` (enrichment / score_rationale / outreach / attribution)."""
    ev = get_owned_event(event_id, user, db)
    _get_attendee(db, ev, aid)
    q = (db.query(models.LLMCall)
           .filter(models.LLMCall.event_id == ev.id,
                   models.LLMCall.attendee_id == aid))
    if purpose:
        q = q.filter(models.LLMCall.purpose == purpose)
    rows = q.order_by(models.LLMCall.created_at.desc()).all()
    return [LLMCallOut(
        id=r.id, purpose=r.purpose, model=r.model, status=r.status,
        prompt=r.prompt or "", output=r.output or "", error=r.error,
        latency_ms=r.latency_ms, created_at=r.created_at,
    ) for r in rows]


@router.get("/{event_id}/curation/features")
def get_features(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
) -> dict:
    """Current NEAR-TERM feature-flag snapshot. UI calls this to decide
    which gated panels to render."""
    get_owned_event(event_id, user, db)
    return {"flags": features.all_flags()}


# ── NEAR-TERM endpoints (gated) ────────────────────────────────────────
# Every handler below calls features.require(<name>) first : returns 404
# when the flag is off so we don't expose them by default.


class NewsSignalBody(BaseModel):
    signals: list[dict]


@router.post("/{event_id}/curation/near-term/news-signal/{aid}")
def near_term_news_signal(
    event_id: int,
    aid: int,
    body: NewsSignalBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Push public-signal events (funding/launch/award/job_change)
    onto an attendee. Quality-gated by kind."""
    features.require("news_signal")
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    payload = near_term.refresh_news_signal(db, a, raw_signals=body.signals)
    db.commit()
    return payload


class RecognitionBody(BaseModel):
    entries: list[dict]  # [{"name": "...", "email": "...", "list_name": "..."}]


@router.post("/{event_id}/curation/near-term/recognition")
def near_term_recognition(
    event_id: int,
    body: RecognitionBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Cross-reference attendees against an org-uploaded
    recognition list (e.g., 'F100 CTOs', 'Distinguished Engineers')."""
    features.require("proprietary_recognition")
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    flagged = near_term.cross_reference_recognition(db, attendees, body.entries)
    db.commit()
    return {"event_id": ev.id, "checked": len(attendees), "flagged": flagged}


class WarmConnectionBody(BaseModel):
    connector_name: str
    connector_email: str = ""
    strength: float = 0.5
    note: str = ""


@router.post("/{event_id}/curation/near-term/warm-connection/{aid}")
def near_term_warm_connection(
    event_id: int,
    aid: int,
    body: WarmConnectionBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Attach a warm-connection signal : someone in the org's
    network knows this attendee."""
    features.require("warm_connection")
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    payload = near_term.attach_warm_connection(
        db, a,
        connector_name=body.connector_name,
        connector_email=body.connector_email,
        strength=body.strength,
        note=body.note,
    )
    db.commit()
    return payload


@router.post("/{event_id}/curation/near-term/predict-no-show")
def near_term_predict_no_show(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Rule-based no-show prediction across every attendee.
    For over-invited events. Replace with a trained model in LIVE."""
    features.require("yield_prediction")
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    out = []
    for a in attendees:
        p, rationale = near_term.write_no_show(db, a)
        out.append({"attendee_id": a.id, "no_show_probability": round(p, 3),
                    "rationale": rationale})
    db.commit()
    return {"event_id": ev.id, "predicted": len(out),
            "method": "rule_based", "results": out}


class SponsorMatchBody(BaseModel):
    sponsor_profile: dict
    top_n: int = 20


@router.post("/{event_id}/curation/near-term/sponsor-match")
def near_term_sponsor_match(
    event_id: int,
    body: SponsorMatchBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Match each sponsor's buyer profile to attendees,
    output a facilitated intro path."""
    features.require("sponsor_match")
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    return {
        "event_id": ev.id,
        "matches": near_term.match_sponsor_to_attendees(
            body.sponsor_profile, attendees, top_n=body.top_n
        ),
    }


@router.post("/{event_id}/curation/near-term/seating")
def near_term_seating(
    event_id: int,
    table_size: int = 6,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Dinner / table / seating optimization. Placeholder
    round-robin; LIVE version should consume the intro-edge weights."""
    features.require("seating_optimization")
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id,
        models.Attendee.rsvp_status.in_(["rsvp_yes", "attended"]),
    ).all()
    if not attendees:
        attendees = db.query(models.Attendee).filter(
            models.Attendee.event_id == ev.id
        ).all()
    return {
        "event_id": ev.id,
        "tables": near_term.optimize_seating(attendees, table_size=table_size),
        "method": "rule_based",
    }


class SessionRelevanceBody(BaseModel):
    sessions: list[dict]  # [{"id":"s1", "title":"...", "keywords":[...], "target_function":"..."}]


@router.post("/{event_id}/curation/near-term/session-relevance")
def near_term_session_relevance(
    event_id: int,
    body: SessionRelevanceBody,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Attendee-to-session relevance scores."""
    features.require("session_relevance")
    ev = get_owned_event(event_id, user, db)
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == ev.id
    ).all()
    matrix: list[dict] = []
    for s in body.sessions:
        for a in attendees:
            score, trace = near_term.score_attendee_for_session(a, s)
            if score > 0:
                matrix.append({
                    "session_id": s.get("id"),
                    "attendee_id": a.id,
                    "score": round(score, 3),
                    "rule_trace": trace,
                })
    matrix.sort(key=lambda r: -r["score"])
    return {"event_id": ev.id, "matrix": matrix, "method": "rule_based"}


@router.get("/{event_id}/curation/near-term/sponsor-roi")
def near_term_sponsor_roi(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Sponsor ROI rollup. Currently aggregates AttendeeAttribution
    rows across attendees the operator has tagged as sponsor-matched (via the
    sponsor-match endpoint). Without that step it returns zeroes."""
    features.require("sponsor_roi")
    ev = get_owned_event(event_id, user, db)
    # No persistent sponsor-match table yet : caller must POST matches first.
    # As a pragmatic placeholder we treat every attribution row on this event
    # as the rollup set when no sponsor list is provided.
    rows = (db.query(models.AttendeeAttribution)
              .filter(models.AttendeeAttribution.event_id == ev.id).all())
    by_outcome: dict[str, int] = {}
    total = 0
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
        total += r.value or 0
    return {"event_id": ev.id, "attributed": len(rows),
            "outcomes": by_outcome, "total_value": total,
            "method": "rule_based"}


@router.get("/{event_id}/curation/near-term/news-attribution/{aid}")
def near_term_news_attribution(
    event_id: int,
    aid: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Surface stored news signals as candidate attribution
    evidence. Doesn't run Claude : downstream attribute_attendee should."""
    features.require("news_attribution")
    ev = get_owned_event(event_id, user, db)
    a = _get_attendee(db, ev, aid)
    return near_term.news_signal_attribution(a)


@router.get("/{event_id}/curation/near-term/recurring-memory")
def near_term_recurring_memory(
    event_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """[NEAR-TERM] Persisted memory of attendee profiles that produced
    outcomes on the operator's prior events. Feed into next-cycle scoring."""
    features.require("recurring_memory")
    get_owned_event(event_id, user, db)  # auth check
    return {"user_id": user.id,
            "memory": near_term.recurring_memory_for_user(db, user.id, limit=limit),
            "method": "rule_based"}
