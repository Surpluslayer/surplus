"""
routes/settings.py : per-user app settings.

    GET /api/settings   the signed-in user's settings
    PUT /api/settings   update them -- any subset of the three fields below;
                        an omitted field is left unchanged

        autonomy_mode:  'off' | 'ask' | 'auto'
        bar_jurisdiction: USPS 2-letter state code, or null to clear
        practice_area:  one of signal_taxonomy.PRACTICE_AREAS, or null to clear

Autonomy is the per-user control over AGENT-INITIATED sends (the due-nudge
dispatch + the AI auto-reply):

    off  = agent drafts; nothing agent-initiated sends (nudges hold in the
           queue, replies stage as PendingReply)
    ask  = same holding mechanics as off; the Today surface lists what is
           waiting for a one-tap confirm
    auto = agent-initiated sends fire unattended, still under the env master
           (SURPLUS_AUTOMATED_SENDS) as the ops kill switch

Manual sends (send-now / schedule / approve) never pass through these gates,
and the built-in post-accept first follow-up keeps its own master
(SURPLUS_AUTO_FOLLOWUPS).

bar_jurisdiction and practice_area used to be settable only via the demo
cohort generator or a one-off admin script -- a real account signing up
outside a demo had no way to ever set either, which is why Observe's own
account section prints "bar=NOT SET" / "practice_area=NOT SET" forever for a
real user who did everything right. Both are validated here rather than
accepted as free text: bar_jurisdiction feeds the Rule 7.3 solicitation
fail-closed gate (backend/solicitation.py), and practice_area is a lookup key
into signal_taxonomy.SIGNAL_AFFINITY_SEED -- an unrecognized value there
doesn't error, it just silently returns the neutral 0.5 for every signal
category, which is worse than leaving it unset (unset is at least visible in
the log as "NOT SET" rather than looking like a real, differentiated score).

Session auth via the standard current_user dependency; each user can only
read/write their own row.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship.pipeline.send.sender import (
    AUTONOMY_MODES,
    owner_autonomy_mode,
)
from ..auth import current_user
from ..db import get_db
from ..demo.signal_taxonomy import PRACTICE_AREAS

router = APIRouter(prefix="/api/settings", tags=["settings"])

_STATE_CODES = frozenset((
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
))


class SettingsOut(BaseModel):
    autonomy_mode: str
    bar_jurisdiction: Optional[str] = None
    practice_area: Optional[str] = None


class SettingsPut(BaseModel):
    autonomy_mode: Optional[str] = None
    bar_jurisdiction: Optional[str] = None
    practice_area: Optional[str] = None


@router.get("", response_model=SettingsOut)
def get_settings(user: models.User = Depends(current_user)) -> SettingsOut:
    """The signed-in user's settings. autonomy_mode is normalized (anything
    unexpected in the column reads back as 'off')."""
    return SettingsOut(autonomy_mode=owner_autonomy_mode(user),
                       bar_jurisdiction=user.bar_jurisdiction,
                       practice_area=user.practice_area)


@router.put("", response_model=SettingsOut)
def put_settings(
    body: SettingsPut,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
) -> SettingsOut:
    """Update any subset of the three settings; a field left out of the
    request body is unchanged (a null explicitly provided clears it). Only
    canonical values are ever accepted -- anything else is a 422, the columns
    never store junk."""
    if body.autonomy_mode is not None:
        mode = body.autonomy_mode.strip().lower()
        if mode not in AUTONOMY_MODES:
            raise HTTPException(
                422, f"autonomy_mode must be one of {', '.join(AUTONOMY_MODES)}")
        user.autonomy_mode = mode

    if "bar_jurisdiction" in body.model_fields_set:
        code = (body.bar_jurisdiction or "").strip().upper() or None
        if code is not None and code not in _STATE_CODES:
            raise HTTPException(422, "bar_jurisdiction must be a USPS 2-letter "
                                      "state code, or null to clear")
        user.bar_jurisdiction = code

    if "practice_area" in body.model_fields_set:
        area = (body.practice_area or "").strip().lower() or None
        if area is not None and area not in PRACTICE_AREAS:
            raise HTTPException(
                422, f"practice_area must be one of {', '.join(PRACTICE_AREAS)}, or null")
        user.practice_area = area

    db.commit()
    return SettingsOut(autonomy_mode=owner_autonomy_mode(user),
                       bar_jurisdiction=user.bar_jurisdiction,
                       practice_area=user.practice_area)
