"""routes/matching.py — stage 04. Build the symbiotic value graph + groups."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..agents.matcher import build_edges, form_groups

router = APIRouter(prefix="/events", tags=["04 · matching"])


def _confirmed(ev: models.Event) -> list[models.Prospect]:
    return [p for p in ev.prospects if p.status == "rsvp"]


# --- manual RSVP override --------------------------------------------------
# For demo/testing: flip prospect.status -> "rsvp" without round-tripping
# through the LinkedIn webhook. Either bulk (all approved+contacted) or
# specific ids. Idempotent: re-flipping an already-rsvp'd prospect is a no-op.

class RsvpRequest(BaseModel):
    all: bool = False
    prospect_ids: list[int] = []


class RsvpResponse(BaseModel):
    event_id: int
    flipped: int
    already_rsvp: int
    rsvp_total: int
    prospect_ids: list[int]


@router.post("/{event_id}/rsvp", response_model=RsvpResponse)
def mark_rsvp(event_id: int, payload: RsvpRequest, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not payload.all and not payload.prospect_ids:
        raise HTTPException(422, "pass either {all: true} or {prospect_ids: [...]}")

    if payload.all:
        targets = [p for p in ev.prospects
                   if p.status in ("approved", "contacted", "rsvp")]
    else:
        idset = set(payload.prospect_ids)
        targets = [p for p in ev.prospects if p.id in idset]
        missing = idset - {p.id for p in targets}
        if missing:
            raise HTTPException(
                404, f"prospects not in event {event_id}: {sorted(missing)}")

    flipped, already = 0, 0
    for p in targets:
        if p.status == "rsvp":
            already += 1
        else:
            p.status = "rsvp"
            flipped += 1
    db.commit()

    rsvp_total = sum(1 for p in ev.prospects if p.status == "rsvp")
    return RsvpResponse(
        event_id=ev.id,
        flipped=flipped,
        already_rsvp=already,
        rsvp_total=rsvp_total,
        prospect_ids=[p.id for p in targets],
    )


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

    edges = build_edges(attending, event=ev)
    for e in edges:
        db.add(models.MatchEdge(event_id=ev.id, **e))

    groups = form_groups(attending, ev)
    for gid, members in groups.items():
        for p in members:
            p.group_id = gid

    db.commit()
    return schemas.MatchResult.build(ev, attending, edges, groups)


class ExplainRequest(BaseModel):
    a_id: int
    b_id: int


class ExplainResponse(BaseModel):
    a_id: int
    b_id: int
    explanation: str
    grounded_in_llm: bool   # False when the cache miss forces a fallback path


@router.post("/{event_id}/pairs/explain", response_model=ExplainResponse)
def explain_pair_endpoint(event_id: int, payload: ExplainRequest,
                          db: Session = Depends(get_db)):
    """On-demand LLM explanation for one pair.

    Uses the enriched profile data + structured component scores cached
    during the most recent /match call. Cache miss => no LLM call (we
    don't want to silently re-enrich behind the user's back).
    """
    import asyncio
    from ..agents import matcher_lib, pair_explainer

    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    attending = _confirmed(ev)
    enriched = matcher_lib.get_cached_enriched(ev, attending)
    matrix = matcher_lib.get_cached_matrix(ev, attending)
    if enriched is None or matrix is None:
        raise HTTPException(
            409,
            "no cached enrichment for this event — re-run /match first "
            "(the in-process cache is lost on server restart)"
        )

    a_key = f"prospect-{payload.a_id}"
    b_key = f"prospect-{payload.b_id}"
    a_person = enriched.get(a_key)
    b_person = enriched.get(b_key)
    if a_person is None or b_person is None:
        raise HTTPException(404, "one of the prospects is not in the enriched cache")

    pair = next(
        (p for p in matrix.get("pairs", [])
         if {p.get("a_id"), p.get("b_id")} == {a_key, b_key}),
        None,
    )

    text = asyncio.run(pair_explainer.explain_pair(a_person, b_person, pair))
    grounded = not text.startswith("Explanation failed")
    return ExplainResponse(
        a_id=payload.a_id, b_id=payload.b_id,
        explanation=text, grounded_in_llm=grounded,
    )


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
