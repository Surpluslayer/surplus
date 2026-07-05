"""routes/privacy.py : data-subject rights — offboarding export + self-delete.

    GET    /api/me/export   full, secret-free export of everything you own
    DELETE /api/me          permanently delete your account + all data (confirm)

Both are session-authed via `current_user`, so a user can only export or delete
their OWN data. The heavy lifting (serialization, cascade delete, subprocessor
revocation, deletion audit) lives in `backend/retention.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, retention
from ..auth import current_user
from ..db import get_db

router = APIRouter(prefix="/api/me", tags=["privacy"])


@router.get("/export")
def export_my_data(user: models.User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    """Everything this user owns, minus secrets (OAuth tokens, password hash,
    session tokens, wrapped DEKs are excluded by construction)."""
    return retention.export_user_data(db, user.id)


@router.delete("")
def delete_my_account(confirm: bool = Query(
                          default=False,
                          description="must be true to permanently delete"),
                      user: models.User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    """Permanently delete the signed-in user and all their data, and revoke
    connected subprocessor access. Irreversible — requires `?confirm=true`.
    Returns per-category deletion counts as the deletion confirmation."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass ?confirm=true to permanently delete your account and all data.")
    return retention.delete_user_data(db, user.id, actor="self")
