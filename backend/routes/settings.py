"""
routes/settings.py : per-user app settings.

    GET /api/settings   the signed-in user's settings ({autonomy_mode})
    PUT /api/settings   update them (autonomy_mode: 'off' | 'ask' | 'auto')

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
(SURPLUS_AUTO_FOLLOWUPS). Session auth via the standard current_user
dependency; each user can only read/write their own row.
"""
from __future__ import annotations

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

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    autonomy_mode: str


class SettingsPut(BaseModel):
    autonomy_mode: str


@router.get("", response_model=SettingsOut)
def get_settings(user: models.User = Depends(current_user)) -> SettingsOut:
    """The signed-in user's settings. autonomy_mode is normalized (anything
    unexpected in the column reads back as 'off')."""
    return SettingsOut(autonomy_mode=owner_autonomy_mode(user))


@router.put("", response_model=SettingsOut)
def put_settings(
    body: SettingsPut,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
) -> SettingsOut:
    """Set the signed-in user's autonomy mode. Only the three canonical values
    are accepted; anything else is a 422 (the column never stores junk)."""
    mode = (body.autonomy_mode or "").strip().lower()
    if mode not in AUTONOMY_MODES:
        raise HTTPException(
            422, f"autonomy_mode must be one of {', '.join(AUTONOMY_MODES)}")
    user.autonomy_mode = mode
    db.commit()
    return SettingsOut(autonomy_mode=mode)
