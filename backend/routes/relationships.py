"""
routes/relationships.py : read API for the event-native relationship layer.

Surfaces the schema-free timeline + summary built by agents/relationships.py.
Every route is owner-scoped : a prospect is only reachable by the user who owns
its event (same 404-on-not-owned discipline as get_owned_event), so relationship
data never leaks across users.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents import relationships
from ..auth import current_user
from ..db import get_db

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
    rows = [relationships.contact_summary(db, c, inter_index) for c in contacts]
    rows.sort(key=lambda r: r["last_touch_at"] or _MIN_DT, reverse=True)
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
