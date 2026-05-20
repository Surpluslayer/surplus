"""routes/roi.py : stage 05. Settle the verified conversion ledger + net ROI."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import current_user, get_owned_event
from ..db import get_db
from ..agents.roi import settle
# Gap #1+#2+#3a: inbound applicants bridged into the ROI shape via the
# adapter, no schema or roi.py edits.
from ..triage.matcher_adapter import is_inbound_event, run_inbound_roi

router = APIRouter(prefix="/events", tags=["05 · roi"])


@router.get("/{event_id}/roi", response_model=schemas.RoiResult)
def get_roi(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Per-guest conversion ledger + aggregate net ROI, settled against the event
    goal. Persists a Conversion row per guest so the ledger is queryable later.
    """
    ev = get_owned_event(event_id, user, db)

    # Gap #3a: inbound events settle ROI in memory only — Conversion rows
    # FK to prospects.id and applicants live in their own table.
    if is_inbound_event(ev):
        result = run_inbound_roi(ev)
        if result is None:
            raise HTTPException(
                409, "no accepted applicants to settle : review the CSV first",
            )
        _attending, ledger, metrics = result
        return schemas.RoiResult.build(ev, ledger, metrics)

    attending = [p for p in ev.prospects if p.status == "rsvp"]
    if not attending:
        raise HTTPException(409, "no confirmed guests to settle : run the pipeline first")

    ledger, metrics = settle(ev, attending)

    # upsert: clear prior conversions, write fresh ones
    for p in attending:
        if p.conversion is not None:
            db.delete(p.conversion)
    db.flush()
    for row in ledger:
        db.add(models.Conversion(
            prospect_id=row["prospect_id"], goal=row["goal"], tier=row["tier"],
            state=row["state"], label=row["label"], detail=row["detail"],
            value=row["value"],
        ))
    db.commit()

    return schemas.RoiResult.build(ev, ledger, metrics)
