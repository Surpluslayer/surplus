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
from sqlalchemy import func
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
                  limit: int = 60, offset: int = 0,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)) -> dict:
    """The owner's accounts, starred first then strongest first. `tier`
    filters exactly; `q` matches the (overlay-corrected) company name or a
    previewed member name, case-insensitive. Overlay-rejected groupings are
    dropped entirely (account_summary returns None for them).

    PAGED AT THE DB (limit default 60, ordered by the materialized starred/
    strength columns): account_summary costs several queries per account, so
    summarizing a whole 400-account book in one request took ~60s on prod and
    timed out the tab. The page summarizes only what the tab can show;
    `total` carries the full count for the pager. A `q` search widens to the
    whole book by company name at the DB before summarizing, so search still
    finds unstarred long-tail accounts."""
    base = (db.query(models.Account)
              .filter(models.Account.owner_type == "user",
                      models.Account.owner_id == user.id))
    if tier:
        base = base.filter(models.Account.tier == tier)
    total = base.count()

    ordered = base.order_by(models.Account.starred.desc(),
                            models.Account.strength_score.desc().nullslast(),
                            models.Account.id)
    if q:
        # Search is hybrid but BOUNDED: company canonical_name matches in SQL
        # across the whole book, plus member-name matches within the top-200
        # summarized accounts. (Whole-book member search would re-open the
        # summarize-everything cost this pagination exists to kill.)
        needle = f"%{q.strip().lower()}%"
        sql_hits = (ordered.join(models.Company,
                                 models.Company.id == models.Account.company_id)
                           .filter(func.lower(models.Company.canonical_name)
                                   .like(needle))
                           .limit(min(limit, 200)).all())
        scan = ordered.limit(200).all()
        seen, accounts = set(), []
        for a in [*sql_hits, *scan]:
            if a.id not in seen:
                seen.add(a.id)
                accounts.append(a)
    else:
        accounts = ordered.offset(offset).limit(min(limit, 200)).all()

    # Lazy rollup heal, page-bounded: accounts born from a bulk backfill can
    # land with NULL strength/count; healing only the visible page keeps the
    # request cheap no matter how large the book is.
    dirty = False
    for account in accounts:
        if account.strength_score is None or not account.contact_count:
            accounts_read.recompute_rollups(db, account)
            dirty = True

    # Batched page assembly (~6 queries total): per-account assembly paid a
    # cross-region round-trip per query and turned a 60-row page into ~47s.
    summaries = accounts_read.account_summaries_page(db, accounts, user.id)
    if q:
        needle_txt = q.strip().lower()
        summaries = [s for s in summaries
                     if any(needle_txt in (v or "").lower()
                            for v in [s["company"]["canonical_name"],
                                      *s["member_preview"]])]

    # Stable in-page order (the DB pre-ordered; healing may have re-scored).
    summaries.sort(key=lambda s: (
        not s["starred"],
        -(s["rollups"]["strength_score"] if s["rollups"]["strength_score"]
          is not None else -1.0),
        (s["company"]["canonical_name"] or "").lower(),
    ))
    if dirty:
        db.commit()
    return {"accounts": summaries, "total": total,
            "limit": min(limit, 200), "offset": offset}


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
