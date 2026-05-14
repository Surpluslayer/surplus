"""routes/events.py — stage 01, intake. Create and read the event profile."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(prefix="/events", tags=["01 · intake"])


@router.post("", response_model=schemas.EventOut, status_code=201)
def create_event(payload: schemas.EventCreate, db: Session = Depends(get_db)):
    """Define the event mechanism. Returns the profile + derived funnel target."""
    ev = models.Event(**payload.model_dump())
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return schemas.EventOut.of(ev)


@router.get("/{event_id}", response_model=schemas.EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    return schemas.EventOut.of(ev)
