"""
routes/inperson.py : the in-person "scan-to-connect" entry point.

A real-event companion to the prospecting pipeline. The operator is standing in
front of someone : they scan a LinkedIn "My Code" QR, paste a profile link, or
type a name. We resolve that to a LinkedIn identity, capture it as a "pending"
Prospect on a lightweight in_person Event, draft a warm post-meeting note, and
let the operator send it through the SAME warm/cold send path as /invite.

All routes require a signed-in user (current_user) and respect UNIPILE_DRY_RUN
(via the per-user / preview providers : dry-run never touches the network).

HARD RULE : free text NEVER auto-sends and NEVER auto-creates a Prospect. The
only way a typed name becomes a Prospect is the operator CONFIRMING a candidate
from /resolve and POSTing its linkedin_url to /scan. /resolve is resolve-only.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents import resolver
from ..agents.outreach import compose_inperson
from ..agents.send_flow import route_and_send
from ..auth import (
    current_user,
    get_owned_event,
    require_linkedin_connected,
    require_outreach_enabled,
)
from ..db import get_db
from ..providers import get_preview_provider, get_provider_for_user

router = APIRouter(prefix="/api/inperson", tags=["in-person"])


# ── request bodies ─────────────────────────────────────────────────────────

class InPersonEventIn(BaseModel):
    label: str
    city: str = ""


class ResolveIn(BaseModel):
    method: str                       # "url" | "text"
    linkedin_url: Optional[str] = None
    name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None


class ScanIn(BaseModel):
    event_id: int
    linkedin_url: str
    source: str                       # "scan" | "link" | "text"
    note: Optional[str] = None
    # Optional enrichment carried over from a confirmed /resolve candidate so
    # the captured Prospect (and its draft) isn't just a bare handle.
    name: Optional[str] = None
    role: Optional[str] = None
    company: Optional[str] = None


class SendIn(BaseModel):
    note: Optional[str] = None
    message: Optional[str] = None


# ── helpers ────────────────────────────────────────────────────────────────

def _handle_from_url(url: str) -> str:
    """Extract the LinkedIn vanity handle from a canonical /in/<handle> URL."""
    return (url or "").rstrip("/").split("/")[-1]


def _owned_prospect(db: Session, prospect_id: int, user: models.User) -> models.Prospect:
    p = db.get(models.Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "capture not found")
    ev = p.event
    if ev is None or getattr(ev, "user_id", None) != user.id:
        raise HTTPException(404, "capture not found")
    return p


def _capture_row(p: models.Prospect) -> dict:
    """CRM-view serialization for one captured Prospect."""
    last = None
    if p.outreach:
        latest = max(p.outreach, key=lambda o: o.ts)
        last = {"state": latest.state, "ts": latest.ts}
    return {
        "prospect_id": p.id,
        "name": p.name,
        "role": p.role,
        "company": p.company,
        "linkedin_url": p.linkedin_url,
        "status": p.status,
        "connection_status": p.connection_status,
        "source": p.source,
        "captured_at": p.captured_at,
        # No dedicated column : an unresolved capture is exactly one with no
        # provider id, so the UI can surface a "retry resolve" affordance.
        "resolve_failed": p.linkedin_provider_id is None,
        "last_outreach": last,
        "conversion": p.conversion.state if p.conversion else None,
    }


# ── routes ─────────────────────────────────────────────────────────────────

@router.post("/events")
def create_or_fetch_inperson_event(
    body: InPersonEventIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Create or fetch the user's in_person Event for `label`. Idempotent : the
    same operator scanning at the same event reuses one Event row."""
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(422, "label is required")
    ev = (db.query(models.Event)
            .filter_by(user_id=user.id, kind="in_person", label=label)
            .first())
    created = False
    if ev is None:
        ev = models.Event(
            user_id=user.id, kind="in_person", label=label,
            city=(body.city or "").strip(),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        created = True
    return {"event_id": ev.id, "label": ev.label, "city": ev.city, "created": created}


@router.post("/resolve")
def resolve_identity(
    body: ResolveIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Resolve an input to a LinkedIn identity. NEVER creates a Prospect, NEVER
    sends : resolve-only.

      method "url"  -> single high-confidence hit (resolve_by_url)
      method "text" -> ranked candidate list (resolve_by_text), never auto-picked
    """
    method = (body.method or "").strip().lower()
    if method == "url":
        if not (body.linkedin_url or "").strip():
            raise HTTPException(422, "linkedin_url is required for method 'url'")
        provider = get_preview_provider(user)
        try:
            hit = resolver.resolve_by_url(body.linkedin_url, provider)
        except Exception as exc:  # noqa: BLE001 : resolve must not 500
            return {"method": "url", "resolved": False,
                    "error": f"{type(exc).__name__}: {exc}", "candidate": None}
        return {"method": "url", "resolved": True, "candidate": hit}

    if method == "text":
        if not (body.name or "").strip():
            raise HTTPException(422, "name is required for method 'text'")
        candidates = resolver.resolve_by_text(
            body.name or "", body.title or "", body.company or "")
        # Empty list -> caller surfaces a "type the link instead" fallback.
        return {"method": "text", "count": len(candidates),
                "candidates": candidates}

    raise HTTPException(422, "method must be 'url' or 'text'")


@router.post("/scan")
def scan_capture(
    body: ScanIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Capture a now-known linkedin_url as a pending Prospect (UPSERT on
    (event_id, linkedin_provider_id)) and return a warm draft. Never sends.

    Resolve failure is non-fatal : we still store the pending capture (so the
    operator doesn't lose it) and flag resolve_failed so it can be retried.
    """
    ev = get_owned_event(body.event_id, user, db)

    canonical = resolver.normalize_linkedin_url(body.linkedin_url) or (
        body.linkedin_url or "").strip()
    if not canonical:
        raise HTTPException(422, "linkedin_url is required")

    provider = get_preview_provider(user)
    provider_id: Optional[str] = None
    resolve_failed = False
    try:
        provider_id = provider.resolve_linkedin_user(canonical)
    except Exception:  # noqa: BLE001 : never 500 on a flaky lookup
        resolve_failed = True

    # UPSERT : prefer matching on the resolved provider id, else on the URL so
    # a re-scan of the same person doesn't create a duplicate.
    p = None
    if provider_id:
        p = (db.query(models.Prospect)
               .filter_by(event_id=ev.id, linkedin_provider_id=provider_id)
               .first())
    if p is None:
        p = (db.query(models.Prospect)
               .filter_by(event_id=ev.id, linkedin_url=canonical)
               .first())

    handle = _handle_from_url(canonical)
    if p is None:
        p = models.Prospect(
            event_id=ev.id,
            identity=handle or canonical,
            name=(body.name or "").strip() or handle or "Unknown",
            linkedin_url=canonical,
            sources="inperson",
        )
        db.add(p)

    # Apply / refresh the capture fields on every scan.
    if provider_id:
        p.linkedin_provider_id = provider_id
    if body.name and body.name.strip():
        p.name = body.name.strip()
    if body.role and body.role.strip():
        p.role = body.role.strip()
    if body.company and body.company.strip():
        p.company = body.company.strip()
    p.status = "pending"
    p.source = (body.source or "").strip() or None
    p.captured_at = datetime.now(timezone.utc)
    p.note = (body.note or None)
    db.commit()
    db.refresh(p)

    draft = compose_inperson(p, ev)
    return {
        "prospect": _capture_row(p),
        "resolve_failed": resolve_failed,
        "draft_note": draft.note,
        "draft_message": draft.message,
    }


@router.get("/events/{event_id}/captures")
def list_captures(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """CRM view : every captured Prospect on this in_person event."""
    ev = get_owned_event(event_id, user, db)
    rows = sorted(
        ev.prospects,
        key=lambda p: (p.captured_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return {"event_id": ev.id, "count": len(rows),
            "captures": [_capture_row(p) for p in rows]}


@router.post("/captures/{prospect_id}/send")
def send_capture(
    prospect_id: int,
    body: SendIn = SendIn(),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send the connect request / DM for ONE captured prospect, through the
    SHARED warm/cold send helper. Honors operator note/message overrides."""
    p = _owned_prospect(db, prospect_id, user)
    if not p.linkedin_url:
        raise HTTPException(409, "capture has no linkedin_url")

    require_outreach_enabled()         # 503 when SURPLUS_KILL_OUTREACH is on
    require_linkedin_connected(user)
    provider = get_provider_for_user(user)

    outcome = route_and_send(
        db, p, provider, p.event,
        note=body.note or None,
        message=body.message or None,
    )
    res = outcome.res
    return {
        "prospect_id": p.id,
        "prospect_name": p.name,
        "linkedin_url": p.linkedin_url,
        "provider": res.provider,
        "dry_run": res.dry_run,
        "state": res.state,
        "provider_lead_id": res.provider_lead_id,
        "error": res.error,
        "note_preview": outcome.final_note if outcome.path_taken == "cold" else None,
        "message_preview": outcome.final_message,
        "connection_status": outcome.connection_status,
        "path_taken": outcome.path_taken,
    }
