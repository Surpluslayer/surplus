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
from sqlalchemy.orm import Session, selectinload

from .. import billing_plans as bp
from .. import models
from ..agents.relationship.spine import relationships
from ..auth import current_user
from ..db import SessionLocal, get_db
from ..integrations.unipile_config import unipile_creds

router = APIRouter(prefix="/api/relationships", tags=["relationships"])

# Sorts never-touched / timeless relationships to the END when sorting newest
# touch first (reverse=True).
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _enforce_relationship_quota(db: Session, user: models.User) -> None:
    """Roll the user into the current billing period, then HARD-BLOCK (402) if
    they've exhausted their drafting or contact-scan budget for the period.

    Demo + allowlisted accounts bypass entirely (bp.is_unlimited), so live
    demos never hit a wall mid-run. The SPA reads detail.error + detail.redirectTo
    to bounce the user to the pricing table. Kept separate from the legacy
    paid_at LinkedIn-send gate."""
    if bp.ensure_current_period(user):
        db.commit()
    if not bp.can_generate_draft(user):
        raise HTTPException(
            status_code=402,
            detail={"error": "LIMIT_REACHED", "redirectTo": "/billing",
                    "message": "You've used all your follow-up drafts for this "
                               "period. Upgrade to keep going.",
                    "billing": bp.usage_snapshot(user)})
    if not bp.can_scan_contacts(user, 1):
        raise HTTPException(
            status_code=402,
            detail={"error": "CONTACT_LIMIT_REACHED", "redirectTo": "/billing",
                    "message": "You've reached your contact-scan limit for this "
                               "period. Upgrade to scan more.",
                    "billing": bp.usage_snapshot(user)})


def _record_relationship_usage(db: Session, user: models.User, res) -> None:
    """Meter one relationship run: +1 per staged DRAFT card, +contacts_seen for
    the triage scan. Best-effort — a metering failure must never break an
    otherwise-successful run, so we swallow + roll back on error."""
    try:
        drafts = sum(1 for p in res.proposals if p.kind == "draft_message")
        contacts = int(getattr(res, "contacts_seen", 0) or 0)
        if drafts or contacts:
            bp.record_usage(user, drafts=drafts, contacts=contacts)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"  [billing] usage record failed: {type(exc).__name__}: {exc}")
        db.rollback()


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


def _fire_booking_after_send(db, user, contact, booking_payload, text: str):
    """Fire the calendar booking a meeting-proposal draft carries, AFTER its
    message sent. Booking is a side effect of SENDING the draft (manual host send
    here; the cron does the auto-send equivalent). Never raises and never affects
    the send's success: a booking miss (no contact email, no open slot) just means
    the message went out without the auto-created invite. Returns the booking
    result dict to surface on the response, or None when there's no payload."""
    if not booking_payload:
        return None
    from ..agents.relationship.pipeline.send.sender import fire_booking_on_send
    try:
        topic = (text or "Quick chat").strip().split("\n", 1)[0][:80] or "Quick chat"
        return fire_booking_on_send(db, user, contact, booking_payload, topic=topic)
    except Exception:  # noqa: BLE001 : a booking miss never fails a sent message
        return None


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
           .filter(models.Event.user_id == user.id)
           # Eager-load the per-row links relationship_summary() reads, so a big
           # book is 3 batched queries instead of N+1 lazy loads : a 500-person
           # book must not 524 behind Cloudflare's 100s cap.
           .options(
               selectinload(models.Prospect.conversion),
               selectinload(models.Prospect.outreach),
               selectinload(models.Prospect.event),
           ))
    if event_id is not None:
        q = q.filter(models.Prospect.event_id == event_id)
    if contact_type:
        q = q.filter(models.Prospect.contact_type == contact_type)

    rows = []
    # Sane cap : the biggest realistic book still renders ; an unbounded scan
    # cannot hang a request until the edge times it out.
    for p in q.limit(1000).all():
        summary = relationships.relationship_summary(p)
        if stage and summary["relationship_stage"] != stage:
            continue
        rows.append({"prospect": _prospect_brief(p),
                     "relationship_summary": summary})

    rows.sort(key=lambda r: r["relationship_summary"]["last_touch_at"] or _MIN_DT,
              reverse=True)
    return {"count": len(rows), "relationships": rows}


@router.post("/email/sync")
def sync_email(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Pull who the user actually corresponds with from their connected
    mailbox into the Contact spine (see agents/email_sync.py). Synchronous —
    a few Unipile pages — so the Integrations tile can await real counts.
    409 until a mailbox is connected. Also auto-kicked once by the email
    connect webhook, so most users never need to call this by hand."""
    if not getattr(user, "unipile_email_account_id", None):
        raise HTTPException(409, "no email account connected")
    from ..agents.relationship.email_sync import sync_email_contacts
    dsn, api_key = _unipile_cfg()
    stats = sync_email_contacts(db, user, dsn=dsn, api_key=api_key)
    return {"ok": stats.get("error") is None, **stats}


class EmailSendIn(BaseModel):
    """One outbound email to a contact, from the host's connected mailbox."""
    message: str
    subject: Optional[str] = None  # default derived from the shared event


@router.post("/contacts/{contact_id}/send-email")
def send_contact_email(
    contact_id: int,
    body: EmailSendIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send `message` to one owned contact AS AN EMAIL from the host's own
    connected mailbox (their Unipile GOOGLE/OUTLOOK seat — never a shared
    account). The email twin of /contacts/{id}/followup.

    Gates: the contact must have a known email (prospect.email or the
    Contact spine's), and the host must have a connected, active mailbox —
    except in dry-run, where the payload is built but nothing leaves the
    box (demos exercise the full path). The per-channel double-send guard
    applies: an unconfirmed email send blocks a blind retry."""
    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")
    prospect = _sendable_prospect(contact)

    to_address = ((getattr(prospect, "email", None) or "").strip().lower()
                  or (contact.email or "").strip().lower())
    if not to_address:
        raise HTTPException(409, "no email address on file for this contact")

    from ..providers import get_provider_for_user
    provider = get_provider_for_user(user)
    email_account_id = getattr(user, "unipile_email_account_id", None) or ""
    if not provider.dry_run:
        if not email_account_id or \
                getattr(user, "email_status", "") != "active":
            raise HTTPException(
                409, "connect your email in Integrations before sending")

    from ..agents.relationship.pipeline.send.flow import _assert_no_recent_send
    if not provider.dry_run:
        _assert_no_recent_send(db, prospect, channel="email")

    ev = getattr(prospect, "event", None)
    label = (getattr(ev, "label", "") or "").strip() if ev else ""
    subject = (body.subject or "").strip() or (
        f"Great meeting you at {label}" if label else "Great meeting you")

    # PUSH-in-thread: when the host confirmed a thread for this contact,
    # reply to its LATEST message (reply_to + matching 'Re:' subject is
    # Unipile's threading contract) so Gmail/Outlook keep one conversation.
    reply_to = None
    if contact.email_thread_id and not provider.dry_run:
        try:
            from ..agents.relationship.email_sync import thread_messages
            dsn, api_key = _unipile_cfg()
            msgs = thread_messages(
                dsn=dsn, api_key=api_key, account_id=email_account_id,
                thread_id=contact.email_thread_id,
                own_address=getattr(user, "email_account_address", "") or "")
            if msgs:
                last = msgs[-1]
                reply_to = last.get("provider_id")
                orig = (last.get("subject") or "").strip()
                if orig:
                    subject = orig if orig.lower().startswith("re:") \
                        else f"Re: {orig}"
        except Exception as exc:  # noqa: BLE001 : fall back to a fresh email
            print(f"  [email.send] thread lookup failed, sending fresh: "
                  f"{type(exc).__name__}: {exc}")

    from ..agents.relationship.email_sync import format_email_html
    to_first = ((contact.name or prospect.name or "").split() or [""])[0]
    host_first = ((user.name or "").split() or [""])[0]
    res = provider.send_email(
        email_account_id=email_account_id,
        to_address=to_address,
        to_name=(contact.name or prospect.name or ""),
        subject=subject,
        body=format_email_html(text, to_first, host_first),
        prospect_id=prospect.id,
        reply_to=reply_to,
    )

    # Truthful log on the email channel : message_sent / unconfirmed / failed
    # (dry runs log dry_run_queued). Same discipline as sender.send_and_log.
    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="email",
        state=res.state,
        body=f"[{subject}] {text}"[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    db.commit()

    if res.error and res.state == "failed":
        raise HTTPException(502, f"email send failed: {res.error}")
    return {"status": "unconfirmed" if res.state == "unconfirmed" else "sent",
            "dry_run": res.dry_run, "contact_id": contact_id,
            "prospect_id": prospect.id, "to": to_address, "subject": subject}


def _unipile_cfg() -> tuple[str, str]:
    creds = unipile_creds()
    if not creds:
        raise HTTPException(503, "Unipile not configured")
    return creds


def _email_channel_ready(user) -> str:
    """The user's email account id, 409ing when no mailbox is connected."""
    acct = getattr(user, "unipile_email_account_id", None)
    if not acct or getattr(user, "email_status", "") != "active":
        raise HTTPException(409, "connect your email in Integrations first")
    return acct


@router.get("/contacts/{contact_id}/email-threads")
def list_contact_email_threads(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Candidate mailbox threads with this contact's address — what the host
    picks from to CONFIRM 'this is my thread with them'. Manual by design:
    we never guess the thread, the host links it."""
    contact = _owned_contact(db, contact_id, user)
    if not (contact.email or "").strip():
        raise HTTPException(409, "no email address on file for this contact")
    acct = _email_channel_ready(user)
    dsn, api_key = _unipile_cfg()
    from ..agents.relationship.email_sync import list_threads_for_address
    threads = list_threads_for_address(
        dsn=dsn, api_key=api_key, account_id=acct,
        address=contact.email.strip().lower(),
        own_address=getattr(user, "email_account_address", "") or "")
    return {"contact_id": contact_id, "address": contact.email,
            "linked_thread_id": contact.email_thread_id, "threads": threads}


class ContactEmailIn(BaseModel):
    email: Optional[str] = None  # null clears


@router.post("/contacts/{contact_id}/email")
def set_contact_email(
    contact_id: int,
    body: ContactEmailIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Manually attach (or clear) this contact's email address — the host
    types it in on the contact view. This is THE entry point of the email
    channel for a contact: thread listing, pull, and push all key off it.
    Changing the address clears any linked thread (it belonged to the old
    address). Also backfills the linked prospects so capture-side surfaces
    (and link_contact identity) see it."""
    contact = _owned_contact(db, contact_id, user)
    addr = (body.email or "").strip().lower() or None
    if addr is not None and "@" not in addr:
        raise HTTPException(422, "that doesn't look like an email address")
    if addr != (contact.email or None):
        contact.email_thread_id = None  # old thread belonged to the old address
    contact.email = addr
    for p in (getattr(contact, "prospects", None) or []):
        if addr and not getattr(p, "email", None):
            p.email = addr
    db.commit()
    return {"contact_id": contact_id, "email": contact.email,
            "linked_thread_id": contact.email_thread_id}


class StarIn(BaseModel):
    vip: Optional[bool] = None  # null = toggle; true/false = set explicitly


@router.post("/import-conversations", status_code=200)
def import_conversations(
    want: int = 15,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Seed the Book from the user's genuine LinkedIn DM conversations (people
    they actually replied to and had an active back-and-forth with). Idempotent
    -- re-runs only add new people. Uses the user's OWN connected account.

    Used to run the whole import INLINE : up to 80 chats x 3 sequential
    Unipile GETs each (12s timeout apiece) before this returned, so the button
    could spin for minutes. Now it queues a Job and returns the id
    immediately; the work runs detached (jobs.execute_import_conversations, on
    its own DB session) and the frontend polls
    GET /import-conversations/{job_id} for progress + the final stats."""
    from .. import jobs as jobs_mod
    job = jobs_mod.new_job(db, event_id=None, user_id=user.id,
                           kind="import_conversations")
    # prefer_modal : the walk can take minutes, so let it survive a web-worker
    # recycle when USE_MODAL is on (local daemon thread otherwise).
    runner = jobs_mod.run_detached(
        jobs_mod.execute_import_conversations, job.id,
        prefer_modal=True,
        want=max(1, min(want, 30)))
    return {"job_id": job.id, "status": "queued", "runner": runner}


@router.get("/import-conversations/{job_id}")
def import_conversations_status(
    job_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Poll one import job. Owner-scoped : 404 unless the job belongs to the
    requesting user and is a conversation import. While running, `progress`
    carries {scanned, found} beats from the chat walk; when done, `result`
    carries the import stats ({imported, considered, reason?})."""
    job = db.get(models.Job, job_id)
    if (job is None or job.kind != "import_conversations"
            or job.user_id != user.id):
        raise HTTPException(404, "import job not found")
    out: dict = {"job_id": job.id, "status": job.status}
    if job.status == "done" and job.result_json:
        try:
            out["result"] = json.loads(job.result_json)
        except (ValueError, TypeError):
            # A truncated / corrupt result_json must not 500 the poller ; hand
            # back the raw payload so the import screen can still resolve.
            out["result_raw"] = job.result_json
    elif job.status in ("queued", "running") and job.result_json:
        try:
            out["progress"] = json.loads(job.result_json)
        except ValueError:
            pass
    if job.status == "error":
        out["error"] = job.error
    return out


class ChannelIn(BaseModel):
    channel: Optional[str] = None  # "email" | "linkedin" | null (auto)


@router.post("/contacts/{contact_id}/channel")
def set_contact_channel(
    contact_id: int,
    body: ChannelIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Set which channel to follow up with this contact on. Drafts + sends honor
    it (email pulls the real email thread for context). null = auto-default."""
    contact = _owned_contact(db, contact_id, user)
    ch = (body.channel or "").strip().lower() or None
    if ch not in (None, "email", "linkedin"):
        raise HTTPException(400, "channel must be 'email', 'linkedin', or null")
    contact.preferred_channel = ch
    db.commit()
    return {"contact_id": contact_id, "preferred_channel": ch}


def _kick_vip_scrape(db, contact_id: int) -> None:
    """Detached one-off scrape for a just-starred contact (run via
    jobs.run_detached). Top-level so it stays importable; `db` is the
    run_detached-owned session. Best-effort."""
    from ..agents.relationship.updates_engine import scrape_contact
    c = db.get(models.Contact, contact_id)
    if c is not None:
        print(f"[star] kicked scrape for contact={contact_id}: "
              f"{scrape_contact(db, c)}", flush=True)


@router.post("/contacts/{contact_id}/star")
def set_contact_star(
    contact_id: int,
    body: StarIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Star / unstar a contact. Starred (⭐ vip) contacts are monitored more
    often by the updates engine. `vip` null → toggle; true/false → set."""
    contact = _owned_contact(db, contact_id, user)
    contact.vip = (not contact.vip) if body.vip is None else bool(body.vip)
    db.commit()
    # On star (not unstar), kick a one-off update check in the background so
    # close-monitoring starts now instead of waiting for the next sweep. Best
    # effort, its own session; never blocks or fails the toggle.
    if contact.vip and (contact.linkedin_url or "").strip():
        from ..jobs import run_detached
        run_detached(_kick_vip_scrape, contact_id)
    return {"contact_id": contact_id, "vip": contact.vip}


class ThreadLinkIn(BaseModel):
    thread_id: Optional[str] = None  # null unlinks


@router.post("/contacts/{contact_id}/email-thread")
def link_contact_email_thread(
    contact_id: int,
    body: ThreadLinkIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The host's manual confirmation: set (or clear) the ONE mailbox thread
    that belongs to this contact. Pull and push both key off it."""
    contact = _owned_contact(db, contact_id, user)
    contact.email_thread_id = (body.thread_id or "").strip() or None
    db.commit()
    return {"contact_id": contact_id,
            "linked_thread_id": contact.email_thread_id}


@router.get("/contacts/{contact_id}/email-thread")
def read_contact_email_thread(
    contact_id: int,
    with_bodies: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """PULL: the linked thread's messages, oldest first (live read from the
    mailbox — nothing stored). 409 until the host has linked a thread."""
    contact = _owned_contact(db, contact_id, user)
    if not contact.email_thread_id:
        raise HTTPException(409, "no email thread linked for this contact")
    acct = _email_channel_ready(user)
    dsn, api_key = _unipile_cfg()
    from ..agents.relationship.email_sync import thread_messages
    msgs = thread_messages(
        dsn=dsn, api_key=api_key, account_id=acct,
        thread_id=contact.email_thread_id,
        own_address=getattr(user, "email_account_address", "") or "",
        with_bodies=with_bodies)
    return {"contact_id": contact_id,
            "thread_id": contact.email_thread_id, "messages": msgs}


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


@router.get("/contacts/due")
def contacts_due(
    within_days: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Who's overdue for a touch -- the relationship-MAINTENANCE surface, most
    overdue first. Cadence (vip / stage -> an expected interval) compared against
    days-since-last-touch; `within_days` looks ahead so a daily sweep can surface
    contacts coming due. Complements the dated-trigger feed (birthdays/flights);
    whether a nudge auto-fires is gated separately by the automation flag.

    Declared BEFORE /contacts/{contact_id} so the literal 'due' isn't captured by
    the int path param. Owner-scoped."""
    from ..agents.relationship.pipeline.proactive import cadence
    rows = cadence.due_contacts(db, user.id, within_days=within_days, limit=limit)
    return {"count": len(rows), "due_contacts": rows}


@router.get("/due")
def relationship_due(
    within_days: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The unified PROACTIVE feed: everything due for the caller right now --
    relationship maintenance (cadence) + dated triggers (birthday, an upcoming
    flight). Read-only; consumes nothing. The surface the UI and the harness pull
    to decide who to reach out to. `within_days` looks ahead."""
    from ..agents.relationship.pipeline import proactive
    return proactive.collect_due(db, user.id, within_days=within_days,
                                 cadence_limit=limit)


@router.get("/plan")
def relationship_plan(
    within_days: int = 1,
    limit: int = 25,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Today's outreach plan: one deduplicated, prioritized list across cadence +
    dated triggers. A birthday outranks staleness; a contact due for both shows
    once. The single 'who should I reach out to' surface for the UI/harness."""
    from ..agents.relationship.pipeline import proactive
    return proactive.daily_plan(db, user.id, within_days=within_days, limit=limit)


@router.get("/_status")
def relationship_status(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Health snapshot of the deterministic relationship layer for the caller:
    fact-store coverage, the proactive due queue, automation-flag state, scheduler
    heartbeats. A development/debugging surface for 'what does the agent know?'."""
    from ..agents.relationship import observability
    return observability.relationship_status(db, user.id)


@router.post("/contacts/dedup")
def contacts_dedup(
    apply: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Find (and with apply=true, MERGE) duplicate Contacts -- the same person split
    across LinkedIn/email identities -- into one canonical row, reassigning every
    prospect/interaction/fact so the gather reads one clean timeline. Owner-scoped.
    Defaults to a DRY RUN (report only); pass apply=true to actually merge."""
    from ..agents.relationship.spine import dedup as contact_dedup
    return contact_dedup.dedup_user(db, user.id, dry_run=not apply)


@router.post("/contacts/{contact_id}/snooze")
def snooze_contact_endpoint(
    contact_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Dismiss a contact from the cadence due-feed for `days` ('not now', no send).
    Their dated triggers (birthday) still surface. Owner-scoped (404 if not owned)."""
    _owned_contact(db, contact_id, user)
    from ..agents.relationship.pipeline.proactive import cadence
    return cadence.snooze_contact(db, user.id, contact_id, days=days)


@router.delete("/contacts/{contact_id}/snooze")
def unsnooze_contact_endpoint(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Clear a contact's cadence snooze so they can surface again. Owner-scoped."""
    _owned_contact(db, contact_id, user)
    from ..agents.relationship.pipeline.proactive import cadence
    return {"cleared": cadence.unsnooze_contact(db, user.id, contact_id)}


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
    is sent here: the host approves a draft separately via the followup route,
    which is where the send-vs-draft decision is made. Owner-scoped."""
    _enforce_relationship_quota(db, user)
    from ..agents.relationship.pipeline.agent.run import (
        run_relationship_agent_concurrent as _run)
    res = _run(db, user.id, instruction=(body.message or "").strip())
    _record_relationship_usage(db, user, res)
    out = res.as_dict()
    # Surface the send-on-approve preference so the chat can label the approve
    # button correctly ("Send now" when on, "Save draft" when off) without a
    # second round-trip. Reads the LEGACY per-user column (nothing writes it
    # anymore, so this is False for new users -> approve stages a draft).
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
    db: Session = Depends(get_db),
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
    # Pre-flight the paywall BEFORE we open the stream: a 402 here is a clean
    # JSON error the SPA can redirect on, whereas raising mid-stream would only
    # surface as an SSE `error` frame after the connection is already open.
    _enforce_relationship_quota(db, user)

    from ..agents.relationship.pipeline.agent.run import (
        run_relationship_agent_concurrent as _run)

    user_id = user.id
    auto = bool(getattr(user, "auto_followups_enabled", False))
    instruction = (body.message or "").strip()
    q: "queue.Queue" = queue.Queue()
    # Set when the client goes away (or the stream completes). The agent
    # checks it before every Claude call, so a closed tab stops the run at
    # the next call boundary instead of silently burning tokens + a DB
    # session to the end of the fan-out.
    stop = threading.Event()

    def _worker():
        from ..agents.relationship.followup_scheduler import suggest_send_time
        db = SessionLocal()
        # One sensible default fire time for this batch; the card prefills its
        # picker with it and the host overrides freely.
        suggested = suggest_send_time().isoformat()
        try:
            def _emit(p):
                if stop.is_set():
                    return  # nobody is reading; don't grow the queue
                q.put(("proposal", {
                    "kind": p.kind, "contact_id": p.contact_id,
                    "contact_name": p.contact_name, "text": p.text,
                    "rationale": p.rationale,
                    "suggested_send_at": suggested,
                }))
            res = _run(db, user_id, instruction=instruction,
                       on_proposal=_emit, stop_event=stop)
            # Meter on the worker's own session/row (the request user can't
            # cross threads). Best-effort; never fails the completed run.
            worker_user = db.get(models.User, user_id)
            if worker_user is not None:
                _record_relationship_usage(db, worker_user, res)
            q.put(("done", {"summary": res.summary or "Done.",
                            "auto_send_enabled": auto}))
        except Exception as exc:  # noqa: BLE001 : surface to the client, don't 500 mid-stream
            q.put(("error", {"message": str(exc)}))
        finally:
            db.close()
            q.put((None, None))

    def _stream():
        # The finally runs on normal completion AND on GeneratorExit — which
        # is what Starlette throws into the generator when the client
        # disconnects mid-stream. Either way, tell the worker to wind down.
        try:
            yield _sse("meta", {"auto_send_enabled": auto})
            threading.Thread(target=_worker, daemon=True).start()
            yield from _drain_stream(q)
        finally:
            stop.set()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        # Defeat proxy buffering so frames arrive as they're produced.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FollowupSendIn(BaseModel):
    """Approve one drafted follow-up for a contact. `message` is the (possibly
    host-edited) body to act on. `channel` picks the transport: "linkedin"
    (default, the historical behavior) or "email" — which routes through the
    contact's stored address + linked thread, no manual typing."""
    message: str
    channel: str = "linkedin"
    subject: Optional[str] = None  # email-only; default derived/Re: threaded


@router.post("/contacts/{contact_id}/followup")
def send_contact_followup(
    contact_id: int,
    body: FollowupSendIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send an approved follow-up draft for one owned contact, immediately.

    Approve = send: this is a manual, user-initiated action, so it bypasses the
    autonomy gates (which only govern unattended sends) and goes out through the
    shared send path (DRY_RUN / paywall enforced inside the provider, exactly
    like the dispatcher). Returns status='sent'.

    Owner-scoped (404 on not-owned contact). The contact is resolved to a
    sendable Prospect by picking its most-recently captured linked prospect."""
    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")

    # Email transport : same approve flow, different wire. Routes through
    # the contact's STORED address (+ linked thread when confirmed), so the
    # agent's drafts can go out as email without the host typing anything.
    if (body.channel or "linkedin").lower() == "email":
        return send_contact_email(
            contact_id, EmailSendIn(message=text, subject=body.subject),
            db, user)

    prospect = _sendable_prospect(contact)

    # Approving a specific message IS the user deciding: a manual send, so it
    # always sends (the autonomy gates only govern UNATTENDED sends). The old
    # legacy-column branch quietly staged a private note instead -- an approve
    # button that does not send. Flipped 2026-07-01 per Daniel.
    from ..agents.relationship.pipeline.send.sender import send_followup
    try:
        res = send_followup(db, prospect, text, channel="linkedin")
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
    channel: str = "linkedin"  # "linkedin" | "email"
    # Structured booking intent for a MEETING-PROPOSAL draft (see
    # integrations.booking.propose_meeting_slot). When present, SENDING this draft
    # also fires the calendar booking: a "propose_time" payload creates the event +
    # invites the contact; a "calendly" payload is self-serve (the link is in the
    # body) so it fires nothing. None for an ordinary follow-up.
    booking_payload: Optional[dict] = None


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

    A SCHEDULED row is auto-fired by the dispatcher only when the general-send
    master (SURPLUS_AUTOMATED_SENDS + channel allowlist) is on; off leaves it
    queued for a manual send-now. We surface `auto_send_enabled` (the legacy
    per-user column) so the card can say 'will send automatically' vs 'queued
    for your confirmation'. An immediate
    'send now' is an explicit host action and always sends. Owner-scoped."""
    from ..agents.relationship.followup_scheduler import pending_followup

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
    want_email = (getattr(body, "channel", "") or "linkedin") == "email"
    booking_payload = getattr(body, "booking_payload", None)
    if send_at is None or send_at <= now:
        from ..agents.relationship.pipeline.send.sender import send_followup
        if want_email:
            try:
                res = send_followup(db, prospect, text, channel="email")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            db.commit()
            if res.error and res.state == "failed":
                raise HTTPException(502, f"email send failed: {res.error}")
            booked = _fire_booking_after_send(db, user, contact, booking_payload, text)
            return {"status": "sent", "contact_id": contact_id,
                    "prospect_id": prospect.id, "channel": "email",
                    "dry_run": res.dry_run, **({"booking": booked} if booked else {})}
        try:
            res = send_followup(db, prospect, text, channel="linkedin")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"send failed: {type(exc).__name__}: {exc}")
        if getattr(res, "error", None):
            raise HTTPException(502, f"send failed: {res.error}")
        booked = _fire_booking_after_send(db, user, contact, booking_payload, text)
        return {"status": "sent", "contact_id": contact_id,
                "prospect_id": prospect.id, "message": text,
                **({"booking": booked} if booked else {})}

    # Schedule: upsert the prospect's one pending row (idempotent per prospect,
    # mirroring stage_followup) so re-approving just reschedules instead of
    # stacking duplicates.
    import json as _json
    payload_str = _json.dumps(booking_payload) if booking_payload else None
    row = pending_followup(db, prospect.id)
    if row is None:
        row = models.ScheduledFollowup(
            prospect_id=prospect.id, body=text, send_at=send_at,
            suggested_send_at=send_at, status="scheduled",
            booking_payload=payload_str)
        db.add(row)
    else:
        row.body = text
        row.send_at = send_at
        row.updated_at = now
        row.booking_payload = payload_str
    row.channel = "email" if want_email else "linkedin"
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
