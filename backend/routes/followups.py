"""
routes/followups.py : the host's control surface for scheduled follow-ups.

The "Gmail Schedule Send" UI for outreach. A follow-up is auto-staged the
moment a first DM goes out (agents/followup_scheduler.stage_followup): a
drafted body + a suggested send time. These routes let the host review that
queue and decide what actually happens:

    GET   /api/followups              list the host's follow-ups
    GET   /api/followups/pending      the "waiting for your OK" queue: rows
                                      that are DUE but held by the autonomy
                                      gate (mode != 'auto' or env master off)
    PATCH /api/followups/{id}         edit the body and/or reschedule send_at
    POST  /api/followups/{id}/cancel  cancel a pending follow-up
    POST  /api/followups/{id}/skip    cancel with reason "skipped" (the ask
                                      mode decline, paired with send-now)
    POST  /api/followups/{id}/send-now  dispatch immediately (the ask mode
                                      one-tap confirm reuses this)

Every route is owner-scoped through the follow-up's prospect -> event -> user,
so one host can never see or touch another host's queue (404 on not-owned,
same no-fingerprinting discipline as get_owned_event).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship.pipeline.send.sender import send_followup
from ..auth import current_user
from ..db import ENGINE, get_db
from ..providers import get_provider

router = APIRouter(prefix="/api/followups", tags=["followups"])


class FollowupOut(BaseModel):
    id: int
    prospect_id: int
    prospect_name: str
    event_id: int
    body: str
    send_at: datetime
    suggested_send_at: datetime
    status: str
    cancel_reason: str
    sent_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class FollowupPatch(BaseModel):
    """Edit a pending follow-up. Both fields optional : send just what changes."""
    body: Optional[str] = None
    send_at: Optional[datetime] = None


def _as_aware(dt: datetime) -> datetime:
    """Treat any naive datetime as UTC : the whole app stores UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _to_out(row: models.ScheduledFollowup) -> FollowupOut:
    p = row.prospect
    return FollowupOut(
        id=row.id,
        prospect_id=row.prospect_id,
        prospect_name=getattr(p, "name", "") or "",
        event_id=getattr(p, "event_id", 0) or 0,
        body=row.body,
        send_at=row.send_at,
        suggested_send_at=row.suggested_send_at,
        status=row.status,
        cancel_reason=row.cancel_reason,
        sent_at=row.sent_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _owned_followup(db: Session, followup_id: int,
                    user: models.User) -> models.ScheduledFollowup:
    """Fetch a follow-up, requiring `user` to own its prospect's event.
    404 in both the not-found and not-owned cases."""
    row = db.get(models.ScheduledFollowup, followup_id)
    if row is None:
        raise HTTPException(404, "follow-up not found")
    event = getattr(row.prospect, "event", None)
    if event is None or getattr(event, "user_id", None) != user.id:
        raise HTTPException(404, "follow-up not found")
    return row


@router.get("", response_model=list[FollowupOut])
def list_followups(
    status: Optional[str] = "scheduled",
    event_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """List the host's follow-ups, newest send_at first. Defaults to the
    pending (scheduled) queue; pass status="" for every status."""
    q = (db.query(models.ScheduledFollowup)
           .join(models.Prospect,
                 models.ScheduledFollowup.prospect_id == models.Prospect.id)
           .join(models.Event, models.Prospect.event_id == models.Event.id)
           .filter(models.Event.user_id == user.id))
    if status:
        q = q.filter(models.ScheduledFollowup.status == status)
    if event_id is not None:
        q = q.filter(models.Prospect.event_id == event_id)
    rows = q.order_by(models.ScheduledFollowup.send_at.asc()).all()
    return [_to_out(r) for r in rows]


class PendingOut(BaseModel):
    """One row of the ask-mode "Waiting for your OK" queue."""
    id: int
    prospect_id: int
    name: str
    message: str
    send_at: datetime
    channel: str


@router.get("/pending", response_model=list[PendingOut])
def list_pending_followups(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The signed-in user's due-but-held follow-ups: rows still `scheduled`
    whose send_at has passed, i.e. what the dispatcher held because the
    autonomy gate is closed (mode 'off'/'ask', or the env master off). The
    ask-mode Today surface renders these for a one-tap Send / Skip.

    Follow-ups only: pending AGENT REPLIES (PendingReply) have no user-scoped
    read today (the approve flow is admin-only in routes/admin.py), so they
    are deliberately not exposed here."""
    now = datetime.now(timezone.utc)
    rows = (db.query(models.ScheduledFollowup)
              .join(models.Prospect,
                    models.ScheduledFollowup.prospect_id == models.Prospect.id)
              .join(models.Event, models.Prospect.event_id == models.Event.id)
              .filter(models.Event.user_id == user.id,
                      models.ScheduledFollowup.status == "scheduled")
              .order_by(models.ScheduledFollowup.send_at.asc())
              .all())
    out: list[PendingOut] = []
    for r in rows:
        if _as_aware(r.send_at) > now:
            continue
        out.append(PendingOut(
            id=r.id,
            prospect_id=r.prospect_id,
            name=getattr(r.prospect, "name", "") or "",
            message=r.body,
            send_at=r.send_at,
            channel=(getattr(r, "channel", "") or "linkedin"),
        ))
    return out


@router.patch("/{followup_id}", response_model=FollowupOut)
def update_followup(
    followup_id: int,
    patch: FollowupPatch,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Edit the draft and/or reschedule. Only a pending follow-up is editable :
    a sent/cancelled one is immutable history."""
    row = _owned_followup(db, followup_id, user)
    if row.status != "scheduled":
        raise HTTPException(409, f"follow-up is {row.status}, not editable")

    if patch.body is not None:
        body = patch.body.strip()
        if not body:
            raise HTTPException(400, "body cannot be empty")
        row.body = body
    if patch.send_at is not None:
        row.send_at = _as_aware(patch.send_at)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/{followup_id}/cancel", response_model=FollowupOut)
def cancel_followup(
    followup_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Cancel a pending follow-up so it never sends."""
    row = _owned_followup(db, followup_id, user)
    if row.status != "scheduled":
        raise HTTPException(409, f"follow-up is {row.status}, cannot cancel")
    row.status = "cancelled"
    row.cancel_reason = "user"
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/{followup_id}/skip", response_model=FollowupOut)
def skip_followup(
    followup_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The ask-mode decline: cancel a held follow-up so it never sends,
    recorded distinctly (reason "skipped") from a queue-screen cancel
    (reason "user"). Owner-scoped 404 like every other route here."""
    row = _owned_followup(db, followup_id, user)
    if row.status != "scheduled":
        raise HTTPException(409, f"follow-up is {row.status}, cannot skip")
    row.status = "cancelled"
    row.cancel_reason = "skipped"
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/{followup_id}/send-now", response_model=FollowupOut)
def send_followup_now(
    followup_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Dispatch a pending follow-up right now instead of waiting for send_at.
    Same send path as the cron, so the row flips to sent/failed identically."""
    row = _owned_followup(db, followup_id, user)
    if row.status != "scheduled":
        raise HTTPException(409, f"follow-up is {row.status}, cannot send")

    prospect = row.prospect
    if prospect is None or prospect.event is None:
        raise HTTPException(409, "follow-up has no prospect/event")
    text = (row.body or "").strip()
    if not text:
        raise HTTPException(400, "follow-up body is empty")

    # ── Atomic claim : make the manual send-now and the cron mutually exclusive
    # on this row. Re-fetch under a row lock (Postgres) and re-check the status:
    # if the cron (or a double-tap) already flipped it to sending/sent/failed
    # since _owned_followup read it, 409 instead of sending a second copy. Then
    # flip "scheduled" -> "sending" and COMMIT before the network send, so the
    # cron's _due_followups (status=="scheduled" only) can never re-pick it.
    q = db.query(models.ScheduledFollowup).filter(
        models.ScheduledFollowup.id == followup_id)
    if ENGINE.dialect.name == "postgresql":
        q = q.with_for_update()
    locked = q.one()
    if locked.status != "scheduled":
        raise HTTPException(409, f"follow-up is {locked.status}, cannot send")
    now = datetime.now(timezone.utc)
    locked.status = "sending"
    locked.updated_at = now
    db.commit()
    row = locked

    try:
        res = send_followup(
            db, prospect, text,
            channel=(getattr(row, "channel", "") or "linkedin"),
            commit=False,
            fallback_provider=get_provider(),
        )
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.cancel_reason = type(exc).__name__
        row.updated_at = now
        db.commit()
        raise HTTPException(502, f"send failed: {type(exc).__name__}: {exc}")

    if res.error:
        row.status = "failed"
        row.cancel_reason = "send_error"
        row.updated_at = now
        db.commit()
        db.refresh(row)
        raise HTTPException(502, f"send failed: {res.error}")

    row.status = "sent"
    row.sent_at = now
    row.updated_at = now
    # An explicit host send-now is the manual approve of this draft: if it carried
    # a meeting booking payload, fire the calendar event + invite now. Never fails
    # the send (no contact email / no open slot just skips the auto-create).
    _fire_followup_booking(db, prospect, getattr(row, "booking_payload", None), text)
    db.commit()
    db.refresh(row)
    return _to_out(row)


def _fire_followup_booking(db, prospect, booking_payload, text: str) -> None:
    """Fire the booking a SENT meeting-proposal follow-up carries (manual send-now).
    Resolves host + Contact from the prospect, delegates to fire_booking_on_send.
    Never raises: a booking miss must not fail a follow-up that already sent."""
    if not booking_payload:
        return
    try:
        from ..agents.relationship.pipeline.send.sender import fire_booking_on_send
        owner = getattr(getattr(prospect, "event", None), "user", None)
        contact = (db.get(models.Contact, prospect.contact_id)
                   if getattr(prospect, "contact_id", None) else None)
        if owner is None or contact is None:
            return
        topic = (text or "Quick chat").strip().split("\n", 1)[0][:80] or "Quick chat"
        fire_booking_on_send(db, owner, contact, booking_payload, topic=topic)
    except Exception:  # noqa: BLE001
        pass
