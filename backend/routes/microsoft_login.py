"""routes/microsoft_login.py : Sign in with Microsoft (the Outlook / 365 login).

    GET /api/auth/microsoft/login     -> {url}  (or ?redirect=1 to 302 to consent)
    GET /api/auth/microsoft/callback  -> exchange, find-or-create, mint a session

Thin wrapper over the shared per-provider flow in routes/_oauth_login (same decoupled
model + cross-client sessions as Google; one User per person via the shared find-or-create).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..integrations import microsoft_login
from . import _oauth_login

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/microsoft/login")
def microsoft_login_start(request: Request, client: str = "web", redirect: int = 0):
    return _oauth_login.login_url(microsoft_login, request, client=client, redirect=redirect)


@router.get("/microsoft/callback")
def microsoft_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return _oauth_login.callback(microsoft_login, request, db, code=code, state=state, error=error)
