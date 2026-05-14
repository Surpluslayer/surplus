"""routes/pipeline.py — stage 02-03. Run the prospecting + outreach pipeline."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..pipeline import run_pipeline

router = APIRouter(prefix="/events", tags=["02-03 · pipeline"])


@router.post("/{event_id}/run", response_model=schemas.PipelineResult)
async def run(event_id: int, db: Session = Depends(get_db)):
    """
    Concurrent fan-out -> fit scoring -> floating threshold -> autonomous
    outreach. Idempotent: re-running clears the prior run first.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    # idempotent — wipe any prior run for this event
    for p in list(ev.prospects):
        db.delete(p)
    db.commit()

    prospects = await run_pipeline(db, ev)
    return schemas.PipelineResult.build(ev, prospects)


@router.get("/{event_id}/prospects", response_model=schemas.PipelineResult)
def get_prospects(event_id: int, db: Session = Depends(get_db)):
    """Read the resolved pool without re-running the pipeline."""
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "pipeline has not been run for this event yet")
    return schemas.PipelineResult.build(ev, ev.prospects)
