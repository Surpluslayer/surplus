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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship import capture_enrich, resolver
from ..agents.relationship.spine import relationships
from ..agents.outreach import compose
from ..agents.relationship.pipeline.send.flow import route_and_send
from ..auth import (
    current_user,
    get_owned_event,
    require_can_send_linkedin,
    require_outreach_enabled,
    user_has_linkedin_connected,
)
from ..db import get_db
from ..hosts import is_first_party, is_inperson_host, request_browser_host
from ..jobs import run_detached
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
    contact_type: Optional[str] = None  # "sales"|"hiring"|"investor"|"partner"|"follow_up"|"other"
    next_step: Optional[str] = None     # follow-up woven into the first message
    email: Optional[str] = None         # their email, if exchanged : unlocks the email channel
    vip: Optional[bool] = None          # icon-only "star this person" toggle
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


def _looks_like_link(text: Optional[str]) -> bool:
    """True if `text` is a scheduling / demo URL (Calendly, cal.com, a bare
    https link, ...) rather than a freeform next-step phrase like 'grab a
    coffee'. Used to decide whether a capture's next_step should be promoted
    to the user's reusable saved_send_link."""
    s = (text or "").strip().lower()
    if not s:
        return False
    return ("http://" in s or "https://" in s
            or "calendly.com" in s or "cal.com" in s
            or "www." in s
            # bare domain-ish token: has a dot and no spaces (e.g. "acme.com/demo")
            or ("." in s and "/" in s and " " not in s))


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
        "vip": bool(getattr(p, "vip", False)),
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
    """Capture a now-known linkedin_url as a pending Prospect (UPSERT on the
    canonical URL) and return FAST. Never sends.

    The slow half (Unipile resolve + LLM enrichment + draft compose) used to
    run inline here, holding the response for three sequential network calls
    while the operator stood in front of the person. It now runs detached
    (finish_scan_capture, on its own DB session) and the response comes back
    with draft_status "pending"; the UI polls GET /scan/{id}/draft for the
    composed copy. The provider resolve is deferred too : the send path
    re-resolves lazily when linkedin_provider_id is missing.
    """
    ev = get_owned_event(body.event_id, user, db)

    canonical = resolver.normalize_linkedin_url(body.linkedin_url) or (
        body.linkedin_url or "").strip()
    if not canonical:
        raise HTTPException(422, "linkedin_url is required")

    # UPSERT on the canonical URL so a re-scan of the same person doesn't
    # create a duplicate. (The provider-id lookup happens in the detached
    # worker now; normalize_linkedin_url keeps the URL key stable.)
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
    # Email, when exchanged at capture : only overwrite with a plausible
    # address, never blank an existing one on a re-scan that omitted it.
    if body.email and "@" in body.email:
        p.email = body.email.strip().lower()
    if body.vip is not None:
        p.vip = bool(body.vip)

    # "Captured once, reused forever" : if this capture's next step is a real
    # scheduling / demo URL, promote it to the user's reusable saved_send_link
    # so it pre-fills on every future send. Latest link wins; freeform phrases
    # ("grab a coffee") never clobber a saved link.
    if _looks_like_link(body.next_step):
        link = body.next_step.strip()
        if (user.saved_send_link or "") != link:
            user.saved_send_link = link

    # Cheap, offline-only slice of enrichment : recover a display name from the
    # vanity handle so the immediate response never shows a bare handle. The
    # full LLM enrichment (title / firm / email) runs in the detached worker.
    if capture_enrich._is_placeholder(p.name, handle):
        quick_name = capture_enrich.name_from_handle(handle)
        if quick_name:
            p.name = quick_name

    # Every scan (first capture or re-scan with a new fun fact) re-drafts, so
    # flip the stored draft to pending; the detached worker flips it to ready.
    p.draft_status = "pending"
    p.draft_note = None
    p.draft_message = None

    db.commit()
    db.refresh(p)

    # Spine: an in-person capture is a real "we met" touch, so link this person
    # to their durable Contact (idempotent, fail-soft, no-op without a strong
    # identity key) so they show up in the cross-event relationship graph.
    # DB-only, so it stays on the fast path; the enrichment back-fill of the
    # Contact happens in the worker once real fields exist.
    relationships.link_contact(db, p, user.id)

    # The slow half : Unipile resolve + LLM enrichment + draft compose, off the
    # request lifecycle on its own DB session (jobs.run_detached).
    run_detached(finish_scan_capture, p.id)

    return {
        "prospect": _capture_row(p),
        # The lookup hasn't been attempted yet; the CRM row's own
        # resolve_failed flag (provider id still missing) is the durable
        # signal once the worker has run.
        "resolve_failed": False,
        "draft_status": p.draft_status or "pending",
        "draft_note": p.draft_note or "",
        "draft_message": p.draft_message or "",
    }


def finish_scan_capture(db: Session, prospect_id: int) -> None:
    """The deferred half of /scan : everything that talks to the network.

    Runs detached (own DB session via jobs.run_detached) so the scan response
    isn't held hostage by Unipile or the LLM:
      1. resolve the LinkedIn URL to the provider id (was a 15s-timeout GET),
      2. prompt-5 enrichment (LLM) so the Book never shows a bare handle,
      3. back-fill the linked Contact from the enriched row,
      4. compose the connection note + first DM, persisted onto the row with
         draft_status "ready" ("failed" on error) for GET /scan/{id}/draft.

    Best-effort throughout : a resolve failure leaves linkedin_provider_id
    NULL (the send path re-resolves lazily, and the CRM surfaces a retry
    affordance via resolve_failed)."""
    p = db.get(models.Prospect, prospect_id)
    if p is None:
        return
    ev = p.event
    if ev is None:
        return
    try:
        owner = ev.user
        if not p.linkedin_provider_id and owner is not None:
            provider = get_preview_provider(owner)
            try:
                provider_id = provider.resolve_linkedin_user(p.linkedin_url or "")
                if provider_id:
                    p.linkedin_provider_id = provider_id
            except Exception:  # noqa: BLE001 : flaky lookup must not kill the draft
                pass

        # Prompt-5 capture enrichment : real name / title / firm before the
        # draft is composed. Fill-only + fail-soft (see capture_enrich).
        capture_enrich.enrich_capture(p, ev)
        db.commit()

        # Back-fill the durable Contact's placeholder name/company now that
        # the row is enriched (link_contact already ran on the fast path).
        contact = relationships.link_contact(db, p, ev.user_id)
        capture_enrich.refresh_contact(db, contact, p)
        db.commit()  # persist the backfill before the (optional) update pull

        # Magic moment: pull this person's freshest public update RIGHT NOW, so
        # someone you just captured surfaces at the top of Today. Best-effort +
        # account-safe (Exa only); a miss (or failure) never touches the draft.
        # (a) Fast path -- Exa web search for an immediate hit, auto-drafted so a
        # found update floats to the TOP of Today (feed sorts drafted + newest).
        try:
            from ..agents.relationship import updates_watch, updates_engine
            emitted = (updates_watch.find_updates(db, contact)
                       if contact is not None else [])
            for change in emitted:
                try:
                    updates_engine.autodraft(db, contact, change)
                except Exception:  # noqa: BLE001
                    pass
            if emitted:
                db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [inperson.finish_scan] capture-update(exa) prospect={prospect_id} "
                  f"skipped: {type(exc).__name__}: {exc}", flush=True)
        # (b) Reliable path -- scrape their ACTUAL LinkedIn profile/posts via
        # Bright Data (async; lands via /webhooks/brightdata in ~a minute). This
        # surfaces a specific person's real recent post when Exa's web index
        # misses it. Best-effort; no-op when Bright Data isn't configured.
        try:
            from ..providers import brightdata
            if (contact is not None and (contact.linkedin_url or "").strip()
                    and brightdata.configured()):
                brightdata.trigger_updates([contact.linkedin_url])
        except Exception as exc:  # noqa: BLE001
            print(f"  [inperson.finish_scan] capture-update(brightdata) "
                  f"prospect={prospect_id} skipped: {type(exc).__name__}: {exc}",
                  flush=True)

        # ev.kind == "in_person", so compose() takes the warm "we just met"
        # branch, grounded in the fun fact + any prior relationship history.
        # Outbound-safe : the context never carries the private_note.
        rel_ctx = relationships.relationship_context(
            p, relationships.fetch_interactions(db, p))
        draft = compose(p, ev, relationship_ctx=rel_ctx)
        p.draft_note = (draft.note or "")[:400]
        p.draft_message = draft.message or ""
        p.draft_status = "ready"
        db.commit()
    except Exception as exc:  # noqa: BLE001 : detached work is best-effort
        db.rollback()
        print(f"  [inperson.finish_scan] prospect={prospect_id} FAILED: "
              f"{type(exc).__name__}: {exc}", flush=True)
        try:
            p.draft_status = "failed"
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()


@router.get("/scan/{prospect_id}/draft")
def scan_draft(
    prospect_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Poll target for the detached draft : the UI renders the capture from
    /scan's fast response, then polls HERE until the composed note + first
    message land. Same auth as /scan (signed-in owner of the capture)."""
    p = _owned_prospect(db, prospect_id, user)
    status = p.draft_status or (
        "ready" if (p.draft_note or p.draft_message) else "failed")
    return {
        "prospect_id": p.id,
        "status": status,                       # "pending" | "ready" | "failed"
        "note": p.draft_note or "",
        "message": p.draft_message or "",
        # Enrichment may have upgraded these while the worker ran.
        "name": p.name,
        "role": p.role,
        "company": p.company,
        "resolve_failed": p.linkedin_provider_id is None,
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

    # The detached scan worker already composed and persisted this capture's
    # draft : hand it to route_and_send so a send with no operator override
    # doesn't re-run the LLM inline (the old behavior when note/message came
    # in empty).
    stored_draft = None
    if p.draft_status == "ready" and (p.draft_note or p.draft_message):
        from ..agents.outreach import Message
        stored_draft = Message(note=p.draft_note or "",
                               message=p.draft_message or "")

    # Cheap latency win : the live connection-status refresh is a ~10s Unipile
    # GET on every send. Skip it when the row was checked in the last few
    # minutes (bulk check-connections or a just-finished send stamped it);
    # the webhook still flips status on acceptance, and _refresh_connection_
    # status already tolerates stale values by design (it keeps the last known
    # status on provider errors).
    checked_at = p.connection_checked_at
    if checked_at is not None and checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    status_is_fresh = (
        p.connection_status in ("connected", "not_connected")
        and checked_at is not None
        and datetime.now(timezone.utc) - checked_at < timedelta(minutes=3)
    )

    try:
        outcome = route_and_send(
            db, p, provider, p.event,
            note=send_note,
            message=body.message or None,
            draft=stored_draft,
            # Trust a fresh status for latency; an already-connected person whose
            # status is stale-wrong is still recovered inside route_and_send (the
            # invite fails, a live check confirms the relation, and it sends the
            # DM instead of a 500).
            refresh_connection=not status_is_fresh,
        )
    except HTTPException as he:
        if he.status_code < 500:
            raise  # clean, intentional 4xx (recent-send 409, note-too-long 400)
        db.rollback()
        return {"prospect_id": p.id, "prospect_name": p.name,
                "linkedin_url": p.linkedin_url, "state": "failed", "dry_run": False,
                "error": str(getattr(he, "detail", he)), "path_taken": None}
    except Exception as exc:  # noqa: BLE001 : never surface a raw 500 to the send UI
        db.rollback()
        return {"prospect_id": p.id, "prospect_name": p.name,
                "linkedin_url": p.linkedin_url, "state": "failed", "dry_run": False,
                "error": f"Couldn't send: {type(exc).__name__}: {exc}",
                "path_taken": None}
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
