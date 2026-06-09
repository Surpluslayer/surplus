"""
routes/relationships.py : read API for the event-native relationship layer.

Surfaces the schema-free timeline + summary built by agents/relationships.py.
Every route is owner-scoped : a prospect is only reachable by the user who owns
its event (same 404-on-not-owned discipline as get_owned_event), so relationship
data never leaks across users.
"""
from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents import relationships
from ..auth import current_user
from ..db import SessionLocal, get_db

router = APIRouter(prefix="/api/relationships", tags=["relationships"])

# Sorts never-touched / timeless relationships to the END when sorting newest
# touch first (reverse=True).
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _owned_contact(db: Session, contact_id: int, user: models.User) -> models.Contact:
    """Fetch a Contact, requiring `user` to own it. 404 in both the not-found
    and not-owned cases so we never leak another user's relationship graph."""
    c = db.get(models.Contact, contact_id)
    if c is None or getattr(c, "user_id", None) != user.id:
        raise HTTPException(404, "contact not found")
    return c


def _sendable_prospect(contact: models.Contact) -> models.Prospect:
    """Resolve a Contact to the per-event Prospect a follow-up acts through:
    the most-recently captured linked prospect that still has an owning event
    (send_and_log / scheduling both need prospect.event). 409 if none."""
    linked = [p for p in (getattr(contact, "prospects", None) or [])
              if getattr(p, "event", None) is not None]
    if not linked:
        raise HTTPException(409, "contact has no sendable event prospect")
    linked.sort(key=lambda p: getattr(p, "captured_at", None) or _MIN_DT,
                reverse=True)
    return linked[0]


def _owned_prospect(db: Session, prospect_id: int, user: models.User) -> models.Prospect:
    """Fetch a Prospect, requiring `user` to own its event. 404 in both the
    not-found and not-owned cases so we never leak another user's prospects."""
    p = db.get(models.Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "prospect not found")
    ev = p.event
    if ev is None or getattr(ev, "user_id", None) != user.id:
        raise HTTPException(404, "prospect not found")
    return p


def _prospect_brief(p: models.Prospect) -> dict:
    """Small, safe identity subset : enough for the timeline header, nothing
    sensitive beyond what the CRM already exposes to the host."""
    return {
        "prospect_id": p.id,
        "name": p.name,
        "role": p.role,
        "company": p.company,
        "headline": p.headline,
        "linkedin_url": p.linkedin_url,
        "status": p.status,
        "connection_status": p.connection_status,
        "contact_type": p.contact_type,
        "source": p.source,
        "captured_at": p.captured_at,
    }


class NoteIn(BaseModel):
    summary: str
    title: str = "Note"
    visibility: str = "private"      # "private" | "team"


class StarIn(BaseModel):
    """Flip a contact's 'important person' star on or off."""
    starred: bool


@router.get("/prospects")
def list_relationships(
    event_id: Optional[int] = None,
    stage: Optional[str] = None,
    contact_type: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Every relationship the user has built across their events — the
    accumulated 'who I've met' list — newest touch first.

    Each row pairs the safe prospect header (no private_note) with its
    relationship_summary, so a 'relationships' view and a 'needs follow-up'
    view both render off this one call. The summary's source_event carries
    which event each person came from.

    Owner-scoped: only the caller's own events are reachable. Optional filters:
      event_id      one event (e.g. a single dinner / conference)
      stage         captured | contacted | replied | converted | stale
      contact_type  sponsor | sales | recruiting | follow_up | ...
    """
    q = (db.query(models.Prospect)
           .join(models.Event, models.Prospect.event_id == models.Event.id)
           .filter(models.Event.user_id == user.id))
    if event_id is not None:
        q = q.filter(models.Prospect.event_id == event_id)
    if contact_type:
        q = q.filter(models.Prospect.contact_type == contact_type)

    rows = []
    for p in q.all():
        summary = relationships.relationship_summary(p)
        if stage and summary["relationship_stage"] != stage:
            continue
        rows.append({"prospect": _prospect_brief(p),
                     "relationship_summary": summary})

    rows.sort(key=lambda r: r["relationship_summary"]["last_touch_at"] or _MIN_DT,
              reverse=True)
    return {"count": len(rows), "relationships": rows}


@router.get("/contacts")
def list_contacts(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The durable 'who I've met' inventory : one row per Contact (the cross-event
    person), rolled up over every event we've shared with them. This is the
    contact-centric counterpart to /prospects (which is per-event-record).

    Owner-scoped : only the caller's own Contacts are reachable. Newest touch
    first, so the people you've engaged most recently surface at the top.
    """
    # Eager-loaded contacts (prospects/event/outreach/conversion in ~5 queries)
    # + a single batched interaction prefetch, so the rollup below is pure
    # in-memory work instead of ~5 queries per prospect (the N+1 that made this
    # page take tens of seconds for a contact-rich user).
    contacts = relationships.list_contacts(db, user.id)
    inter_index = relationships.prefetch_interactions_by_prospect(db, contacts)
    update_index = relationships.prefetch_activity_updates_by_contact(db, contacts)
    rows = [relationships.contact_summary(db, c, inter_index,
                                          update_index.get(c.id))
            for c in contacts]
    # "What's new on top" : order by the freshest signal — the most recent
    # external update if there is one, else the last touch — so contacts the
    # poller just found news about surface first.
    def _freshness(r):
        upd = (r.get("latest_update") or {}).get("occurred_at")
        return max(d for d in (upd, r["last_touch_at"], _MIN_DT) if d is not None)
    rows.sort(key=_freshness, reverse=True)
    return {"count": len(rows), "contacts": rows}


@router.get("/contacts/{contact_id}")
def contact_detail(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The full durable-person profile for one owned Contact : the rollup summary,
    the per-event breakdown ('events we've shared'), and the unified cross-event
    timeline."""
    c = _owned_contact(db, contact_id, user)
    return {
        "contact_summary": relationships.contact_summary(db, c),
        "events": relationships.contact_events(db, c),
        "timeline": relationships.contact_timeline(db, c),
    }


@router.patch("/contacts/{contact_id}/star")
def set_contact_star(
    contact_id: int,
    body: StarIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Mark (or un-mark) an owned contact as an important person.

    The star is the host's explicit 'keep this relationship warm' signal : the
    relationship agent treats a starred contact as a primary follow-up candidate
    (a must-nominate, ranked at the top), so starring directly steers who gets
    drafted next. Owner-scoped (404 on not-owned). Idempotent : setting the same
    value twice is a no-op."""
    c = _owned_contact(db, contact_id, user)
    c.starred = bool(body.starred)
    db.commit()
    return {"contact_id": contact_id, "starred": c.starred}


@router.post("/refresh")
def refresh_crm(
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Manually poll the caller's CRM (Contact spine) for LinkedIn changes —
    job/company moves, headline edits, new posts — and emit each real change as
    an activity_update interaction (which then shows up in GET /updates and the
    per-contact timeline).

    Owner-scoped : only ever touches THIS user's contacts. Read-only against
    LinkedIn (never sends). `limit` caps how many contacts to poll this call
    (oldest-checked first), so a big CRM can be swept in round-robin batches.

    Dispatch mirrors the rest of the app: when USE_MODAL is set we spawn the
    off-box sweep and return immediately; otherwise we run inline and return the
    poll summary so a manual trigger gives instant feedback."""
    from ..jobs import use_modal, _spawn_modal, execute_crm_refresh

    if use_modal() and _spawn_modal("run_crm_refresh", user.id, limit=limit):
        return {"dispatched": "modal", "user_id": user.id}

    summary = execute_crm_refresh(user.id, limit=limit)
    return {"dispatched": "local", **summary}


@router.get("/updates")
def relationship_updates(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The 'what's new' feed : every change the watch-poller has detected about
    the caller's tracked people, newest first. Backed by the append-only
    activity_update RelationshipInteraction rows the refresh job writes.

    Owner-scoped via actor_user_id. Each item carries the contact it's about so
    the feed can render 'Maya changed roles' without a second lookup."""
    limit = max(1, min(limit, 200))
    rows = (
        db.query(models.RelationshipInteraction)
        .filter(models.RelationshipInteraction.actor_user_id == user.id)
        .filter(models.RelationshipInteraction.source_type == "activity_update")
        .order_by(models.RelationshipInteraction.occurred_at.desc())
        .limit(limit)
        .all()
    )
    # Batch-resolve contact names (avoid an N+1 over the feed).
    contact_ids = {r.contact_id for r in rows if r.contact_id}
    names: dict[int, str] = {}
    if contact_ids:
        for c in (db.query(models.Contact)
                    .filter(models.Contact.id.in_(contact_ids)).all()):
            names[c.id] = c.name

    items = [{
        "contact_id": r.contact_id,
        "name": names.get(r.contact_id, ""),
        "type": r.interaction_type,      # job_change | profile_update | new_post
        "title": r.title,
        "summary": r.summary,
        "occurred_at": r.occurred_at,
    } for r in rows]
    return {"count": len(items), "updates": items}


@router.post("/agent/run")
def run_relationship_agent(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Run the propose-only relationship agent over the caller's own contact
    spine. The agent loops — surveys contacts, reads histories, and stages
    next-step / draft-message proposals — but NEVER sends or writes: it
    returns suggestions for the host to approve. Owner-scoped (it only ever
    sees this user's contacts)."""
    from ..agents.relationship_agent import run_relationship_agent as _run
    res = _run(db, user.id)
    return res.as_dict()


class ChatIn(BaseModel):
    """One turn from the host's follow-up chat. `message` is the host's ask
    ('who should I follow up with?', 'draft a ping to anyone at Stripe')."""
    message: str = ""


@router.post("/chat")
def relationship_chat(
    body: ChatIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Conversational front door to the propose-only relationship agent.

    The host types an ask; we steer the same auditable survey-and-propose loop
    with it and hand back (a) a one-paragraph natural-language reply and (b) the
    staged proposals (each a contact + drafted follow-up + rationale). NOTHING
    is sent here — the host approves a draft separately via the followup route,
    which is where the auto-send toggle is honored. Owner-scoped."""
    from ..agents.relationship_agent import (
        run_relationship_agent_concurrent as _run)
    res = _run(db, user.id, instruction=(body.message or "").strip())
    out = res.as_dict()
    # Surface the host's auto-send preference so the chat can label the approve
    # button correctly ("Send now" when on, "Save draft" when off) without a
    # second round-trip.
    out["auto_send_enabled"] = bool(getattr(user, "auto_followups_enabled", False))
    return out


# How often to trickle a keepalive comment while the agent is mid-think and has
# no frame to send. Must stay well under the edge proxy's idle timeout (~30s+ on
# Railway/Cloudflare) so a silent stream never gets cut with a 502.
_HEARTBEAT_SECS = 10


def _sse(event: str, data: dict) -> str:
    """One Server-Sent-Events frame: an event name + a JSON data line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _drain_stream(q: "queue.Queue", *, heartbeat_secs: float = _HEARTBEAT_SECS):
    """Yield SSE bytes off the worker queue until the sentinel (None, None).

    During a silence (the agent mid-think, nothing staged yet) trickle a
    keepalive comment every `heartbeat_secs` so the connection never goes quiet
    long enough for an edge proxy to idle-time-out and 502 the browser. Comment
    frames (": ...") carry no event:/data: line, so the client parser drops them.
    """
    while True:
        try:
            event, data = q.get(timeout=heartbeat_secs)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if event is None:
            return
        yield _sse(event, data)


@router.post("/chat/stream")
def relationship_chat_stream(
    body: ChatIn,
    user: models.User = Depends(current_user),
):
    """Streaming twin of /chat: same propose-only loop, but each drafted
    follow-up is pushed to the client the instant the agent stages it (SSE),
    so the chat reveals people one-by-one as the survey runs instead of
    freezing on a spinner until the whole loop finishes.

    Frames: `meta` (auto-send pref, sent first) -> `proposal` (one per staged
    draft) -> `done` (closing summary) -> `error` (if the run blew up). Still
    NOTHING is sent here; proposals are staged suggestions only. Owner-scoped.

    The agent runs in a worker thread with its OWN DB session (the request's
    session can't cross threads), pushing onto a queue the SSE generator drains.
    user.id is captured up front so the thread never touches the request user.

    The agent runs the two-phase concurrent variant: one triage call, then a
    bounded parallel fan-out of per-person drafts, each card streaming the moment
    its draft resolves. Cards arrive in completion order (not strict priority),
    which is the trade for collapsing time-to-all-cards from Σ to ~max."""
    from ..agents.relationship_agent import (
        run_relationship_agent_concurrent as _run)

    user_id = user.id
    auto = bool(getattr(user, "auto_followups_enabled", False))
    instruction = (body.message or "").strip()
    q: "queue.Queue" = queue.Queue()

    def _worker():
        from ..agents.followup_scheduler import suggest_send_time
        db = SessionLocal()
        # One sensible default fire time for this batch; the card prefills its
        # picker with it and the host overrides freely.
        suggested = suggest_send_time().isoformat()
        try:
            def _emit(p):
                q.put(("proposal", {
                    "kind": p.kind, "contact_id": p.contact_id,
                    "contact_name": p.contact_name, "text": p.text,
                    "rationale": p.rationale,
                    "suggested_send_at": suggested,
                }))
            res = _run(db, user_id, instruction=instruction, on_proposal=_emit)
            q.put(("done", {"summary": res.summary or "Done.",
                            "auto_send_enabled": auto}))
        except Exception as exc:  # noqa: BLE001 : surface to the client, don't 500 mid-stream
            q.put(("error", {"message": str(exc)}))
        finally:
            db.close()
            q.put((None, None))

    def _stream():
        yield _sse("meta", {"auto_send_enabled": auto})
        threading.Thread(target=_worker, daemon=True).start()
        yield from _drain_stream(q)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        # Defeat proxy buffering so frames arrive as they're produced.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FollowupSendIn(BaseModel):
    """Approve one drafted follow-up for a contact. `message` is the (possibly
    host-edited) body to act on."""
    message: str


@router.post("/contacts/{contact_id}/followup")
def send_contact_followup(
    contact_id: int,
    body: FollowupSendIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Act on an approved follow-up draft for one owned contact.

    Behavior is gated by the host's auto-send toggle (User.auto_followups_enabled):
      ON  -> send immediately through the contact's most-recent prospect via the
             shared send_and_log path (DRY_RUN / paywall enforced inside the
             provider, exactly like the follow-up cron). Returns status='sent'.
      OFF -> stage the draft as a private note on the timeline and return
             status='drafted'; nothing leaves the system.

    Owner-scoped (404 on not-owned contact). The contact is resolved to a
    sendable Prospect by picking its most-recently captured linked prospect."""
    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")

    prospect = _sendable_prospect(contact)

    auto_send = bool(getattr(user, "auto_followups_enabled", False))
    if not auto_send:
        # Toggle off: stage the draft as a private note so it shows on the
        # timeline; the host can send it later from the follow-up queue.
        relationships.add_note(
            db, prospect, user.id, text,
            title="Follow-up draft", visibility="private")
        return {"status": "drafted", "contact_id": contact_id,
                "prospect_id": prospect.id, "message": text}

    # Toggle on: send through the same path the follow-up cron uses.
    from ..agents.sender import send_and_log
    from ..providers import get_provider
    try:
        res = send_and_log(
            db, prospect, text,
            sent_state="follow_up_sent",
            fallback_provider=get_provider(),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"send failed: {type(exc).__name__}: {exc}")
    if getattr(res, "error", None):
        raise HTTPException(502, f"send failed: {res.error}")
    return {"status": "sent", "contact_id": contact_id,
            "prospect_id": prospect.id, "message": text}


class FollowupScheduleIn(BaseModel):
    """Schedule (or immediately send) a chat-drafted follow-up for a contact.

    `message` is the (possibly host-edited) body. `send_at` is the host-chosen
    fire time; null/absent or a past time means 'send now'. This is the
    Gmail-style 'Schedule send' the chat cards drive."""
    message: str
    send_at: Optional[datetime] = None


@router.post("/contacts/{contact_id}/schedule")
def schedule_contact_followup(
    contact_id: int,
    body: FollowupScheduleIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Approve a chat-drafted follow-up by SCHEDULING it (or sending now).

    Bridges the propose-only relationship chat into the existing ScheduledFollowup
    queue, so a drafted message becomes a real timed send instead of a dead-end
    private note:

      send_at now/past/absent -> send immediately (send_and_log), status='sent'.
      send_at in the future    -> upsert the prospect's pending ScheduledFollowup
                                   to body + send_at, status='scheduled'.

    The auto-send toggle (User.auto_followups_enabled) still gates a SCHEDULED
    row: the dispatch cron only auto-fires it when auto-send is ON; OFF leaves it
    queued for a manual send-now. We surface `auto_send_enabled` so the card can
    say 'will send automatically' vs 'queued for your confirmation'. An immediate
    'send now' is an explicit host action and always sends. Owner-scoped."""
    from ..agents.followup_scheduler import pending_followup

    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")
    prospect = _sendable_prospect(contact)

    now = datetime.now(timezone.utc)
    send_at = body.send_at
    if send_at is not None and send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=timezone.utc)

    # Send now: no future time chosen. Explicit host action, sends regardless of
    # the auto toggle (same as the followups send-now route).
    if send_at is None or send_at <= now:
        from ..agents.sender import send_and_log
        from ..providers import get_provider
        try:
            res = send_and_log(db, prospect, text, sent_state="follow_up_sent",
                               fallback_provider=get_provider())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"send failed: {type(exc).__name__}: {exc}")
        if getattr(res, "error", None):
            raise HTTPException(502, f"send failed: {res.error}")
        return {"status": "sent", "contact_id": contact_id,
                "prospect_id": prospect.id, "message": text}

    # Schedule: upsert the prospect's one pending row (idempotent per prospect,
    # mirroring stage_followup) so re-approving just reschedules instead of
    # stacking duplicates.
    row = pending_followup(db, prospect.id)
    if row is None:
        row = models.ScheduledFollowup(
            prospect_id=prospect.id, body=text, send_at=send_at,
            suggested_send_at=send_at, status="scheduled")
        db.add(row)
    else:
        row.body = text
        row.send_at = send_at
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return {"status": "scheduled", "contact_id": contact_id,
            "prospect_id": prospect.id, "followup_id": row.id,
            "send_at": row.send_at.isoformat(),
            "auto_send_enabled": bool(getattr(user, "auto_followups_enabled", False)),
            "message": text}


@router.get("/prospects/{prospect_id}/timeline")
def prospect_timeline(
    prospect_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The full relationship timeline + summary for one owned prospect, unioning
    derived touches with stored RelationshipInteraction rows (notes, etc.)."""
    p = _owned_prospect(db, prospect_id, user)
    interactions = relationships.fetch_interactions(db, p)
    return {
        "prospect": _prospect_brief(p),
        "relationship_summary": relationships.relationship_summary(p, interactions),
        "timeline": relationships.build_timeline(p, interactions),
    }


@router.post("/prospects/{prospect_id}/notes")
def create_note(
    prospect_id: int,
    body: NoteIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Record a manual note against an owned prospect (stored as a
    RelationshipInteraction; links the Contact spine opportunistically) and
    return the refreshed timeline."""
    p = _owned_prospect(db, prospect_id, user)
    summary = (body.summary or "").strip()
    if not summary:
        raise HTTPException(422, "summary is required")
    relationships.add_note(db, p, user.id, summary,
                           title=body.title, visibility=body.visibility)
    interactions = relationships.fetch_interactions(db, p)
    return {
        "prospect": _prospect_brief(p),
        "relationship_summary": relationships.relationship_summary(p, interactions),
        "timeline": relationships.build_timeline(p, interactions),
    }
