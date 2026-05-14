"""routes/matching.py — stage 04. Build the symbiotic value graph + groups."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..agents.matcher import build_edges, form_groups

router = APIRouter(prefix="/events", tags=["04 · matching"])


def _confirmed(ev: models.Event) -> list[models.Prospect]:
    return [p for p in ev.prospects if p.status == "rsvp"]


@router.post("/{event_id}/match", response_model=schemas.MatchResult)
def match(event_id: int, db: Session = Depends(get_db)):
    """
    Score every pair of confirmed guests (symbiotic / affinity) and pack them
    into the format's groups, balancing market sides. Idempotent.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    attending = _confirmed(ev)
    if not attending:
        raise HTTPException(409, "no confirmed guests — run the pipeline first")

    # idempotent — clear prior edges + group assignments
    for e in list(ev.edges):
        db.delete(e)
    for p in attending:
        p.group_id = None
    db.flush()

    edges = build_edges(attending)
    for e in edges:
        db.add(models.MatchEdge(event_id=ev.id, **e))

    groups = form_groups(attending, ev)
    for gid, members in groups.items():
        for p in members:
            p.group_id = gid

    db.commit()
    return schemas.MatchResult.build(ev, attending, edges, groups)


@router.get("/{event_id}/matches", response_model=schemas.MatchResult)
def get_matches(event_id: int, db: Session = Depends(get_db)):
    """Read the stored value graph without recomputing it."""
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.edges:
        raise HTTPException(409, "matching has not been run for this event yet")

    attending = _confirmed(ev)
    edges = [{"a_id": e.a_id, "b_id": e.b_id,
              "edge_type": e.edge_type, "weight": e.weight} for e in ev.edges]
    groups: dict[int, list] = {}
    for p in attending:
        if p.group_id is not None:
            groups.setdefault(p.group_id, []).append(p)
    return schemas.MatchResult.build(ev, attending, edges, groups)
