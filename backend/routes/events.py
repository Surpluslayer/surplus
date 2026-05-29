"""routes/events.py : stage 01, intake. Create and read the event profile.

Both routes require auth. Events are owned by the signed-in user; other users
cannot read or write someone else's event (404 in both not-found and
not-owned cases : see auth.get_owned_event).
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, status as http_status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import current_user, get_owned_event
from ..db import get_db

router = APIRouter(prefix="/events", tags=["01 · intake"])


# Per-user event quota : the EventCreate schema accepts {} (defaults match
# the demo so the intake form's auto-submit "just works"). That's a
# convenience for the demo, but it also lets a single authed user
# spam-create thousands of demo-default events. Cap at 200 per user :
# orders of magnitude more than any real operator needs at this stage,
# but tight enough to make automated abuse pointless. Adjust if a real
# customer ever hits the cap.
EVENT_QUOTA_PER_USER = 200


@router.post("", response_model=schemas.EventOut, status_code=201)
def create_event(
    payload: schemas.EventCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Define the event mechanism. Returns the profile + derived funnel target.
    The event is auto-stamped with the signed-in user's id."""
    # Per-user quota guard. Counts events the user already owns ;
    # rejects with 429 (rate limit) rather than 403 so the SPA can
    # show a "you've hit your quota" message instead of "forbidden".
    existing = (db.query(models.Event)
                  .filter(models.Event.user_id == user.id)
                  .count())
    if existing >= EVENT_QUOTA_PER_USER:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "event_quota_exceeded",
                "message": (
                    f"You've reached the per-account cap of "
                    f"{EVENT_QUOTA_PER_USER} events. Delete an old "
                    f"event to make room, or contact us if you're "
                    f"hitting this legitimately."
                ),
            },
        )
    data = payload.model_dump()
    sponsors_payload = data.pop("sponsors", []) or []
    # Multi-select fields arrive as lists; the Event columns are CSV strings.
    for key in ("seniority", "co_stage", "goal", "sources", "yoe"):
        v = data.get(key)
        if isinstance(v, list):
            data[key] = ",".join(s.strip().lower() if key == "sources" else s.strip()
                                 for s in v if s and s.strip())
    # Always force LinkedIn into the stored sources (matches the runtime
    # invariant in adapters_for) so the row is consistent even before the
    # first prospecting run.
    src = (data.get("sources") or "").strip()
    if "linkedin" not in src.split(","):
        data["sources"] = ("linkedin," + src).rstrip(",")
    ev = models.Event(**data, user_id=user.id)
    db.add(ev)
    db.flush()
    # Persist sponsors if any : intake's only condition for sponsor
    # matching to render is "≥1 sponsor exists on the event".
    import json
    for s in sponsors_payload:
        if not (s.get("name") or "").strip():
            continue
        buyer = s.get("buyer_profile") or {}
        db.add(models.Sponsor(
            event_id=ev.id,
            name=s["name"].strip(),
            tier=(s.get("tier") or "").strip(),
            buyer_profile=json.dumps(buyer),
        ))
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
