"""
routes/team_conflicts.py : the conflict-import flow for the team plane
(docs/accounts-architecture.md §6b — deterministic gates, no LLM in v1).

Thin HTTP shell over agents/relationship/conflict_import.py, the same
discipline routes/teams.py keeps toward team_view.py: every gate (parse,
provisional walls, coverage invariant, confirm/skip interlock) lives in the
agent module; this file only does access control and body validation.

Access model matches routes/teams.py exactly (its helpers are imported, not
re-derived): non-members 404 on every route (existence-hiding), members
without the admin role 403 (conflict lists and walls are admin-only in both
directions — the list itself reveals who is conflicted on what).

Separate router, same "/api/teams" prefix: the orchestrator (main.py)
mounts it alongside the teams router; tests mount it standalone.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship import conflict_import
from ..auth import current_user
from ..db import get_db
from .teams import _admin_or_403, _member_or_404

router = APIRouter(prefix="/api/teams", tags=["team-conflicts"])


# ─── request bodies ─────────────────────────────────────────────────────────

class ImportBody(BaseModel):
    text: str = ""


class ConfirmBody(BaseModel):
    confirmed: bool = False


class SkipBody(BaseModel):
    reason: str = ""


# ─── routes ─────────────────────────────────────────────────────────────────

@router.post("/{team_id}/conflicts/import")
def import_conflicts(team_id: int, body: ImportBody,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(current_user)):
    """Deterministic parse + instant provisional name-walls (gates 1-3).
    Every line of the pasted list lands in exactly one audited state and
    every parsed name is walled-by-default before any review; the response
    is the per-line mapping the admin will confirm against."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    if not (body.text or "").strip():
        raise HTTPException(400, "text required")
    return conflict_import.import_text(db, team=team, actor_user_id=user.id,
                                       text=body.text)


@router.get("/{team_id}/conflicts")
def list_conflicts(team_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(current_user)):
    """The review mapping: current provisional name-walls with the live
    companies each one matches (0 = unresolved, 1 = confirmable to an
    entity wall, 2+ = ambiguous, stays over-walled)."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    return conflict_import.review(db, team=team)


@router.post("/{team_id}/conflicts/confirm")
def confirm_conflicts(team_id: int, body: ConfirmBody,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(current_user)):
    """Gate 4/5: the admin confirms the mapping. Single-match name-walls
    become entity walls (name_norm kept as belt-and-braces); zero/multi
    match walls stay name-walls (still enforcing — the safe direction);
    the strict team's view flips pending -> live."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    if not body.confirmed:
        raise HTTPException(400, "confirmed: true required")
    return conflict_import.confirm(db, team=team, actor_user_id=user.id)


@router.post("/{team_id}/conflicts/skip")
def skip_conflicts(team_id: int, body: SkipBody,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(current_user)):
    """The audited skip: go live WITHOUT an import — allowed, but only as
    an explicit, reasoned, logged decision. An empty reason is refused;
    "we skipped conflict screening" must be attributable and justified."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(400, "reason required to skip conflict import")
    return conflict_import.skip(db, team=team, actor_user_id=user.id,
                                reason=reason)
