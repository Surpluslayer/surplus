"""
routes/referral.py : the referral surface for the signed-in user.

    GET  /api/referral/me        share link + banked months + referral list,
                                  plus whether the in-product ask should show
    POST /api/referral/prompted  mark the ask as shown, so it fires once

Session auth via the standard current_user dependency; a user only ever sees
their own code and their own referrals. All the lifecycle logic lives in
backend/referral.py; this module is a thin read/write surface over it.

The share LINK itself (/r/<code>) is a top-level redirect registered in main.py
(it must set the surplus_ref cookie before the OAuth bounce), not here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from .. import referral as referral_lib
from ..auth import current_user, is_demo_user
from ..db import get_db

router = APIRouter(prefix="/api/referral", tags=["referral"])


class ReferralItem(BaseModel):
    name: str
    status: str          # sent | held | rewarded | rejected  (UI vocabulary)
    detail: str          # human line under the name


class ReferralOut(BaseModel):
    code: str
    link: str
    months_banked: int
    should_prompt: bool
    referrals: list[ReferralItem]


# Map the DB status to the UI vocabulary the tracker screen renders.
_UI_STATUS = {
    "pending": "sent",
    "qualified": "held",
    "rewarded": "rewarded",
    "rejected": "rejected",
}


def _share_link(request: Request, code: str) -> str:
    """Absolute share URL on the caller's own host, e.g.
    https://event.surpluslayer.com/r/<code>."""
    host = request.headers.get("host") or "surpluslayer.com"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    return f"{scheme}://{host}/r/{code}"


def _referee_name(db: Session, referee_id: int) -> str:
    u = db.get(models.User, referee_id)
    nm = (getattr(u, "name", "") or "").strip() if u else ""
    return nm or "Someone you referred"


def _detail_for(ref: models.Referral) -> str:
    if ref.status == "rewarded":
        return "Subscribed. Free month banked."
    if ref.status == "qualified":
        hold = referral_lib._aware(ref.hold_until)
        if hold is not None:
            days = max(0, (hold - datetime.now(timezone.utc)).days)
            return f"Paid. Clears in {days} day{'s' if days != 1 else ''}."
        return "Paid. Clearing."
    if ref.status == "rejected":
        return "Not eligible."
    return "Invited. Not joined yet."


@router.get("/me", response_model=ReferralOut)
def my_referrals(request: Request,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)) -> ReferralOut:
    """The signed-in user's referral surface: their share link, months banked,
    every referral they've made, and whether to show the ask right now."""
    code = referral_lib.ensure_referral_code(db, user) or ""
    rows = referral_lib.referrals_for(db, user.id) if not is_demo_user(user) else []
    items = [
        ReferralItem(name=_referee_name(db, r.referee_id),
                     status=_UI_STATUS.get(r.status, r.status),
                     detail=_detail_for(r))
        for r in rows
    ]
    return ReferralOut(
        code=code,
        link=_share_link(request, code),
        months_banked=referral_lib.months_banked(db, user.id),
        should_prompt=referral_lib.should_prompt(user),
        referrals=items,
    )


@router.post("/prompted")
def mark_prompted(db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)) -> dict:
    """The client showed the referral ask: stamp it so it never shows again."""
    referral_lib.mark_prompted(db, user)
    return {"ok": True}
