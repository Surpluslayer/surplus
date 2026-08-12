"""routes/demo_observability.py : the "click an opportunity, see the
machine's actual decision trace" surface -- product feature next to its own
technical observability, both reading real generated rows (backend/demo/).

Gated exactly like routes/demo.py's /enter (same DEMO_ACCESS_TOKEN env var,
same constant-time compare, same 404-not-403-on-a-bad-key posture: probing
this surface with a wrong key is indistinguishable from it being off).
Read-only: nothing here writes, generates, or deletes a cohort -- that's
`python -m backend.demo.cohort` (a deliberate operator action, not an HTTP
side effect).
"""
from __future__ import annotations

import hmac
import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from .. import models
from ..demo import eval_backtest as bt
from ..demo import ranking_trace as rt

router = APIRouter(prefix="/api/demo/observability", tags=["demo-observability"])

_NO_STORE = {"Cache-Control": "no-store"}


def _token() -> Optional[str]:
    tok = (os.environ.get("DEMO_ACCESS_TOKEN") or "").strip()
    return tok or None


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "not found"}, headers=_NO_STORE)


def _check(key: str) -> Optional[JSONResponse]:
    expected = _token()
    if not expected or not hmac.compare_digest(key or "", expected):
        return _not_found()
    return None


@router.get("/cohorts")
def list_cohorts(key: str = Query(...), db: DbSession = Depends(get_db)):
    if (bad := _check(key)) is not None:
        return bad
    rows = db.execute(
        select(models.DemoProvenance.cohort_id, models.DemoProvenance.data_provenance)
        .distinct()
    ).all()
    cohorts: dict[str, list[str]] = {}
    for cohort_id, provenance in rows:
        cohorts.setdefault(cohort_id, []).append(provenance)
    return {"cohorts": [{"cohort_id": c, "provenance": p} for c, p in cohorts.items()]}


@router.get("/opportunities")
def list_opportunities(key: str = Query(...), cohort_id: str = Query(...),
                        user_email: str = Query(...), limit: int = Query(10, le=50),
                        db: DbSession = Depends(get_db)):
    """Ranked opportunities for ONE demo lawyer -- filtered to contacts that
    actually carry a detected signal (source_type == 'activity_update'), so
    "opportunity" means what it says rather than surfacing plain relationship
    warmth with no signal behind it."""
    if (bad := _check(key)) is not None:
        return bad
    user = db.execute(
        select(models.User).where(models.User.email == user_email)
    ).scalar_one_or_none()
    if user is None:
        return JSONResponse(status_code=404, content={"detail": "no such demo lawyer"})

    contact_tags = db.execute(
        select(models.DemoProvenance).where(
            models.DemoProvenance.cohort_id == cohort_id,
            models.DemoProvenance.table_name == "contacts",
        )
    ).scalars().all()
    contacts = db.execute(
        select(models.Contact).where(
            models.Contact.id.in_([t.row_id for t in contact_tags]),
            models.Contact.user_id == user.id,
        )
    ).scalars().all()
    signaled = [c for c in contacts if rt._latest_signal(db, c.id) is not None]

    traces = rt.rank_opportunities(db, user, signaled)
    return {
        "user": {"email": user.email, "practice_area": user.practice_area,
                 "bar_jurisdiction": user.bar_jurisdiction},
        "opportunities": [t.to_dict() for t in traces[:limit]],
    }


@router.get("/trace/{contact_id}")
def get_trace(contact_id: int, key: str = Query(...), user_email: str = Query(...),
              db: DbSession = Depends(get_db)):
    """The full ranking trace for one contact -- the product-feature-vs-
    technical-observability pairing: this endpoint IS the technical half."""
    if (bad := _check(key)) is not None:
        return bad
    user = db.execute(
        select(models.User).where(models.User.email == user_email)
    ).scalar_one_or_none()
    contact = db.get(models.Contact, contact_id)
    if user is None or contact is None or contact.user_id != user.id:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    trace = rt.compute_trace(db, user, contact)
    return trace.to_dict()


@router.get("/feedback/{contact_id}")
def get_feedback_loop(contact_id: int, key: str = Query(...), user_email: str = Query(...),
                       db: DbSession = Depends(get_db)):
    """The viewed -> saved -> drafted -> edited -> sent -> outcome stepper for
    one contact's most recent drafted signal, reconstructed from real
    generated RelationshipInteraction rows -- not a separate log."""
    if (bad := _check(key)) is not None:
        return bad
    user = db.execute(
        select(models.User).where(models.User.email == user_email)
    ).scalar_one_or_none()
    contact = db.get(models.Contact, contact_id)
    if user is None or contact is None or contact.user_id != user.id:
        return JSONResponse(status_code=404, content={"detail": "not found"})

    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact_id)
        .order_by(models.RelationshipInteraction.occurred_at.asc())
    ).scalars().all()

    steps = []
    for r in rows:
        if r.title == "Reviewed contact":
            steps.append({"step": "viewed", "at": r.occurred_at.isoformat()})
        elif r.title and r.title.startswith("Drafted follow-up"):
            meta = json.loads(r.meta_json or "{}")
            steps.append({"step": "drafted", "at": r.occurred_at.isoformat(),
                         "signal_category": meta.get("signal_category")})
            disposition = meta.get("disposition")
            if disposition == "discarded":
                steps.append({"step": "discarded", "at": r.occurred_at.isoformat()})
            else:
                steps.append({"step": "approved", "at": r.occurred_at.isoformat()})
        elif r.title == "Edited draft before sending":
            steps.append({"step": "edited", "at": r.occurred_at.isoformat()})
            steps.append({"step": "sent", "at": r.occurred_at.isoformat()})
    return {"contact_id": contact_id, "contact_name": contact.name, "steps": steps}


@router.get("/eval")
def get_eval(key: str = Query(...), cohort_id: str = Query(...),
             db: DbSession = Depends(get_db)):
    """The v0-naive-vs-v1-full-trace backtest, computed live against real
    generated outcome data (backend/demo/eval_backtest.py) -- never a
    hardcoded pair of numbers."""
    if (bad := _check(key)) is not None:
        return bad
    result = bt.run_backtest(db, cohort_id)
    out = result.to_dict()
    out["caveat"] = ("Computed against this demo cohort's generated data, not live "
                     "customer outcomes -- see backend/demo/distributions.py for what "
                     "is and isn't derived from real telemetry.")
    return out
