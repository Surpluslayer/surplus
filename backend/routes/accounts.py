"""
routes/accounts.py : owner-plane API for the account layer (Accounts tab).

Every route is owner-scoped with the same 404-on-not-owned discipline as
routes/relationships.py — an account is only reachable by the user who owns it
(owner_type="user", owner_id=current user), so account data never leaks across
users. These endpoints are the OWNER's own full-detail view; the team plane
(levels/walls) reads through its own filtered path, never through here.

Registered by the app entrypoint (main.py), not here.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship import accounts_read
from ..auth import current_user
from ..db import get_db

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

_TIERS = {"key", "active", "tracked"}
_SHARING_LEVELS = {"private", "metadata", "elevated"}


def _owned_account(db: Session, account_id: int,
                   user: models.User) -> models.Account:
    """Fetch an Account, requiring `user` to own it. 404 in both the
    not-found and not-owned cases so we never leak that another user's
    account exists (same discipline as _owned_contact)."""
    a = db.get(models.Account, account_id)
    if a is None or a.owner_type != "user" or a.owner_id != user.id:
        raise HTTPException(404, "account not found")
    return a


# ── request bodies ───────────────────────────────────────────────────────────

class AccountPatch(BaseModel):
    tier: Optional[str] = None
    starred: Optional[bool] = None
    objective: Optional[str] = None
    notes: Optional[str] = None
    sharing_level: Optional[str] = None


class OverlayIn(BaseModel):
    canonical_name: Optional[str] = None
    rejected: Optional[bool] = None


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_accounts(tier: Optional[str] = None, q: Optional[str] = None,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)) -> dict:
    """The owner's accounts, starred first then strongest first. `tier`
    filters exactly; `q` matches the (overlay-corrected) company name or a
    previewed member name, case-insensitive. Overlay-rejected groupings are
    dropped entirely (account_summary returns None for them)."""
    query = (db.query(models.Account)
               .filter(models.Account.owner_type == "user",
                       models.Account.owner_id == user.id))
    if tier:
        query = query.filter(models.Account.tier == tier)

    summaries = []
    dirty = False
    for account in query.all():
        # Lazy rollup: accounts born from a bulk backfill land with NULL
        # strength/last-touch (the backfill deliberately skips per-account
        # recompute to stay inside its HTTP batch window). First list view
        # heals them, so the tab never shows a graph of empty chips.
        if account.strength_score is None or not account.contact_count:
            accounts_read.recompute_rollups(db, account)
            dirty = True
        s = accounts_read.account_summary(db, account, user.id)
        if s is None:
            continue
        if q:
            needle = q.strip().lower()
            hay = [s["company"]["canonical_name"], *s["member_preview"]]
            if not any(needle in (v or "").lower() for v in hay):
                continue
        summaries.append(s)

    # Starred first, then strength desc (unscored sinks to the bottom),
    # then name for a stable order.
    summaries.sort(key=lambda s: (
        not s["starred"],
        -(s["rollups"]["strength_score"] if s["rollups"]["strength_score"]
          is not None else -1.0),
        (s["company"]["canonical_name"] or "").lower(),
    ))
    if dirty:
        db.commit()
    return {"accounts": summaries}


@router.get("/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(current_user)) -> dict:
    account = _owned_account(db, account_id, user)
    detail = accounts_read.account_detail(db, account, user.id)
    if detail is None:
        # Viewer rejected this grouping — portrayed as absent, like the list.
        raise HTTPException(404, "account not found")
    return detail


@router.patch("/{account_id}")
def patch_account(account_id: int, body: AccountPatch,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)) -> dict:
    account = _owned_account(db, account_id, user)

    if body.sharing_level is not None and body.sharing_level not in _SHARING_LEVELS:
        raise HTTPException(
            400, f"sharing_level must be one of {sorted(_SHARING_LEVELS)}")
    if body.tier is not None and body.tier not in _TIERS:
        raise HTTPException(400, f"tier must be one of {sorted(_TIERS)}")

    if body.tier is not None:
        account.tier = body.tier
    if body.starred is not None:
        account.starred = bool(body.starred)
    if body.objective is not None:
        account.objective = body.objective
    if body.notes is not None:
        account.notes = body.notes
    if body.sharing_level is not None:
        account.sharing_level = body.sharing_level

    accounts_read.recompute_rollups(db, account)
    db.commit()

    summary = accounts_read.account_summary(db, account, user.id)
    if summary is None:            # patched while overlay-rejected: still ack
        return {"id": account.id, "rejected": True}
    return summary


@router.post("/{account_id}/recompute")
def recompute_account(account_id: int, db: Session = Depends(get_db),
                      user: models.User = Depends(current_user)) -> dict:
    account = _owned_account(db, account_id, user)
    accounts_read.recompute_rollups(db, account)
    db.commit()
    summary = accounts_read.account_summary(db, account, user.id)
    if summary is None:
        return {"id": account.id, "rejected": True}
    return summary


@router.post("/{account_id}/overlay")
def upsert_overlay(account_id: int, body: OverlayIn,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(current_user)) -> dict:
    """Upsert the viewer's per-user correction on this account's company:
    a corrected display name and/or a rejected flag. Global Company rows are
    pipeline-owned — this is the ONLY way a user disagrees with them."""
    account = _owned_account(db, account_id, user)

    overlay = (db.query(models.CompanyOverlay)
                 .filter(models.CompanyOverlay.user_id == user.id,
                         models.CompanyOverlay.company_id == account.company_id)
                 .first())
    if overlay is None:
        overlay = models.CompanyOverlay(user_id=user.id,
                                        company_id=account.company_id)
        db.add(overlay)

    if body.canonical_name is not None:
        try:
            corrections = json.loads(overlay.corrections_json or "{}")
        except (TypeError, ValueError):
            corrections = {}
        name = body.canonical_name.strip()
        if name:
            corrections["canonical_name"] = name
        else:                       # empty string clears the correction
            corrections.pop("canonical_name", None)
        overlay.corrections_json = json.dumps(corrections)
    if body.rejected is not None:
        overlay.rejected = bool(body.rejected)

    db.commit()

    summary = accounts_read.account_summary(db, account, user.id)
    if summary is None:            # just rejected: nothing left to portray
        return {"id": account.id, "rejected": True}
    return summary
