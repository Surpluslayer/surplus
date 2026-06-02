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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents import relationships, resolver
from ..agents.outreach import compose
from ..agents.send_flow import route_and_send
from ..auth import (
    current_user,
    get_owned_event,
    require_can_send_linkedin,
    require_outreach_enabled,
    user_has_linkedin_connected,
)
from ..db import get_db
from ..hosts import is_first_party, is_inperson_host, request_browser_host
from ..providers import get_preview_provider, get_provider_for_user


def _require_send_allowed(request: Request, user: models.User) -> None:
    """Send gate for the in-person surface.

    On the in-person host (event.surpluslayer.com) connecting LinkedIn + sending
    are free : we only require a connected, active LinkedIn account (mechanically
    needed to send). Real sends are still guarded by UNIPILE_DRY_RUN. On any
    other host the full paywall (connected AND paid) applies, same as the
    desktop product. Host is taken from a first-party Origin/X-Forwarded-Host so
    a forged header on the apex can't claim the in-person exemption."""
    host = request_browser_host(request)
    if is_first_party(host) and is_inperson_host(host):
        if not user_has_linkedin_connected(user):
            require_can_send_linkedin(user)  # raises 402 linkedin_send_locked
        return
    require_can_send_linkedin(user)

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
    note: Optional[str] = None          # fun fact : personalizes the draft
    private_note: Optional[str] = None  # operator-only memo : never sent
    contact_type: Optional[str] = None  # "sales"|"recruiting"|"follow_up"|"other"
    next_step: Optional[str] = None     # follow-up woven into the first message
    # Optional enrichment carried over from a confirmed /resolve candidate so
    # the captured Prospect (and its draft) isn't just a bare handle.
    name: Optional[str] = None
    role: Optional[str] = None
    company: Optional[str] = None


class SendIn(BaseModel):
    note: Optional[str] = None
    message: Optional[str] = None
    # "Connect without a note" : send a BARE invite (dodges LinkedIn's 300-char
    # note cap). The personalized DM still fires automatically once accepted.
    # Takes precedence over `note`.
    no_note: bool = False


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
        "note": p.note,                      # fun fact (personalizes the draft)
        "private_note": p.private_note,       # operator-only memo (never sent)
        "contact_type": p.contact_type,
        "next_step": p.next_step,
        # No dedicated column : an unresolved capture is exactly one with no
        # provider id, so the UI can surface a "retry resolve" affordance.
        "resolve_failed": p.linkedin_provider_id is None,
        "last_outreach": last,
        "conversion": p.conversion.state if p.conversion else None,
        # Relationship-aware summary (additive : existing fields above are
        # untouched). Lets the CRM show stage / last-touch / next-step without
        # a second round-trip. See agents/relationships.py.
        "relationship_summary": relationships.relationship_summary(p),
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
    p.note = (body.note or None)                  # fun fact : drives the draft
    p.private_note = (body.private_note or None)   # operator-only : never sent
    p.contact_type = (body.contact_type or None)
    p.next_step = (body.next_step or None)         # woven into the first message
    db.commit()
    db.refresh(p)

    # Spine: an in-person capture is a real "we met" touch, so link this person
    # to their durable Contact (idempotent, fail-soft, no-op without a strong
    # identity key) so they show up in the cross-event relationship graph.
    relationships.link_contact(db, p, user.id)

    # ev.kind == "in_person", so compose() takes the warm "we just met" branch.
    # p.note was just persisted, so the draft is composed FROM the fun fact :
    # re-scanning with an updated note re-personalizes both halves. On a re-scan
    # of someone with prior history, ground the draft in that history too (None
    # on a first capture, so the common path is unchanged). Outbound-safe : the
    # context block never carries the operator-only private_note.
    rel_ctx = relationships.relationship_context(
        p, relationships.fetch_interactions(db, p))
    draft = compose(p, ev, relationship_ctx=rel_ctx)
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


def _is_operator(user: models.User) -> bool:
    """True when this session is the env-var operator account (the single owner
    that rolls up all guest + regular in-person activity)."""
    import os
    op = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    return bool(op) and (getattr(user, "unipile_account_id", None) == op)


@router.get("/activity")
def operator_activity(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Operator-only roll-up of ALL in-person captures across every in_person
    event (guests included), for the activity page on the in-person host.

    Gated to the operator account AND the in-person host : a regular signed-in
    user (or a guest) gets 403, and it only answers on event.surpluslayer.com."""
    host = request_browser_host(request)
    if not is_inperson_host(host):
        raise HTTPException(404, "not found")
    if not _is_operator(user):
        raise HTTPException(403, "operator access required")

    events = (db.query(models.Event)
                .filter(models.Event.kind == "in_person")
                .all())
    # Map owning user -> whether they're a guest (LinkedIn-less anonymous).
    out_events: list[dict] = []
    total = 0
    for ev in sorted(events, key=lambda e: e.id, reverse=True):
        caps = sorted(
            ev.prospects,
            key=lambda p: (p.captured_at or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        owner = ev.user
        is_guest = bool(owner is not None
                        and not getattr(owner, "unipile_account_id", None)
                        and (owner.email or "").endswith("@anonymous.surplus"))
        total += len(caps)
        out_events.append({
            "event_id": ev.id,
            "label": ev.label or ev.event_name or "",
            "city": ev.city,
            "owner": {
                "user_id": getattr(owner, "id", None),
                "name": getattr(owner, "name", None),
                "is_guest": is_guest,
            },
            "captures": [_capture_row(p) for p in caps],
            "count": len(caps),
        })
    return {"events": out_events, "event_count": len(out_events),
            "capture_count": total}


@router.post("/captures/{prospect_id}/send")
def send_capture(
    prospect_id: int,
    request: Request,
    body: SendIn = SendIn(),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send the connect request / DM for ONE captured prospect, through the
    SHARED warm/cold send helper. Honors operator note/message overrides.

    On the in-person host, connect + send are free (gate is connected-only);
    on the apex the full send paywall applies. UNIPILE_DRY_RUN still governs
    whether anything actually leaves the box."""
    p = _owned_prospect(db, prospect_id, user)
    if not p.linkedin_url:
        raise HTTPException(409, "capture has no linkedin_url")

    require_outreach_enabled()         # 503 when SURPLUS_KILL_OUTREACH is on
    _require_send_allowed(request, user)
    provider = get_provider_for_user(user)

    # "Connect without a note" wins over any note text : send a bare invite.
    # route_and_send treats note="" as an explicit empty note (vs None = use
    # the composed draft), so the invite goes out with no note attached.
    send_note = "" if body.no_note else (body.note or None)
    outcome = route_and_send(
        db, p, provider, p.event,
        note=send_note,
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
