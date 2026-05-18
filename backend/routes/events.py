"""routes/events.py : stage 01, intake. Create and read the event profile.

Both routes require auth. Events are owned by the signed-in user; other users
cannot read or write someone else's event (404 in both not-found and
not-owned cases : see auth.get_owned_event).
"""
from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import current_user, get_owned_event
from ..db import get_db

router = APIRouter(prefix="/events", tags=["01 · intake"])


@router.post("", response_model=schemas.EventOut, status_code=201)
def create_event(
    payload: schemas.EventCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Define the event mechanism. Returns the profile + derived funnel target.
    The event is auto-stamped with the signed-in user's id."""
    data = payload.model_dump()
    # Multi-select fields arrive as lists; the Event columns are CSV strings.
    for key in ("seniority", "co_stage", "goal", "enabled_sources"):
        v = data.get(key)
        if isinstance(v, list):
            data[key] = ",".join(s.strip() for s in v if s and s.strip())
    ev = models.Event(**data, user_id=user.id)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return schemas.EventOut.of(ev)


@router.get("/{event_id}", response_model=schemas.EventOut)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    return schemas.EventOut.of(ev)
