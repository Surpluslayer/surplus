"""routes/google_login.py : Sign in with Google (the DECOUPLED login).

    GET /api/auth/google/login     -> {url}  (or ?redirect=1 to 302 to consent)
    GET /api/auth/google/link      -> {url}  (authenticated; link Google to current user)
    GET /api/auth/google/callback  -> exchange, find-or-create/link, mint a session

A brand-new user gets an account here WITHOUT any Unipile/LinkedIn connect. Thin wrapper
over the shared per-provider flow in routes/_oauth_login.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user
from ..db import get_db
from ..integrations import google_login
from . import _oauth_login

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/google/login")
def google_login_start(request: Request, client: str = "web", redirect: int = 0):
    return _oauth_login.login_url(google_login, request, client=client, redirect=redirect)


@router.get("/google/link")
def google_link_start(request: Request, user: models.User = Depends(current_user)):
    """Start linking Google to the CURRENT signed-in account (the safe migration)."""
    return _oauth_login.link_url(google_login, request, user)


@router.get("/google/callback")
def google_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _oauth_login.callback(google_login, request, db, code=code, state=state, error=error)
