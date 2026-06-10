"""
routes/followups.py : the host's control surface for scheduled follow-ups.

The "Gmail Schedule Send" UI for outreach. A follow-up is auto-staged the
moment a first DM goes out (agents/followup_scheduler.stage_followup): a
drafted body + a suggested send time. These routes let the host review that
queue and decide what actually happens:

    GET   /api/followups              list the host's follow-ups
    PATCH /api/followups/{id}         edit the body and/or reschedule send_at
    POST  /api/followups/{id}/cancel  cancel a pending follow-up
    POST  /api/followups/{id}/send-now  dispatch immediately

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
from ..agents.sender import send_and_log
from ..auth import current_user
from ..db import get_db
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


class FollowupSettings(BaseModel):
    """The host's auto-follow-up preference."""
    auto_followups_enabled: bool


class FollowupSettingsPatch(BaseModel):
    enabled: bool


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


@router.get("/settings", response_model=FollowupSettings)
def get_followup_settings(
    user: models.User = Depends(current_user),
):
    """Whether this host has the auto-schedule-follow-up feature turned on."""
    return FollowupSettings(
        auto_followups_enabled=bool(getattr(user, "auto_followups_enabled", False)))


@router.put("/settings", response_model=FollowupSettings)
def set_followup_settings(
    patch: FollowupSettingsPatch,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Turn auto-SEND of follow-ups on or off for this host. Off by default.

    Follow-up drafts are always staged when a first DM goes out regardless of
    this flag : it only controls whether the dispatch cron sends them. Off ->
    drafts wait in the queue for a manual send-now; on -> they send at send_at.
    Turning it off does NOT cancel anything already queued."""
    user.auto_followups_enabled = bool(patch.enabled)
    db.commit()
    return FollowupSettings(auto_followups_enabled=user.auto_followups_enabled)


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

    now = datetime.now(timezone.utc)
    try:
        if (getattr(row, "channel", "") or "linkedin") == "email":
            from ..agents.sender import send_followup_email
            res = send_followup_email(db, prospect, text)
        else:
            res = send_and_log(
                db, prospect, text,
                sent_state="follow_up_sent",
                fallback_provider=get_provider(),
                commit=False,
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
    db.commit()
    db.refresh(row)
    return _to_out(row)
