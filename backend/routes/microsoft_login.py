"""routes/microsoft_login.py : Sign in with Microsoft (the Outlook / 365 login).

    GET /api/auth/microsoft/login     -> {url}  (or ?redirect=1 to 302 to consent)
    GET /api/auth/microsoft/callback  -> exchange, find-or-create User, mint a session

Mirror of routes/google_login -- same decoupled model (no Unipile needed), same
cross-client session (web cookie vs ios/plugin Bearer), sharing find_or_create_oauth_user
so a person who signs in with Google AND Microsoft on the same email is ONE User.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import create_session, find_or_create_oauth_user, set_session_cookie
from ..db import get_db
from ..integrations import microsoft_login
from .auth import _surplus_base_url

router = APIRouter(prefix="/api/auth", tags=["auth"])

_CLIENTS = {"web", "ios", "plugin"}


def _redirect_uri(request: Request) -> str:
    return f"{_surplus_base_url(request)}/api/auth/microsoft/callback"


@router.get("/microsoft/login")
def microsoft_login_start(request: Request, client: str = "web", redirect: int = 0):
    """Return the Microsoft consent URL (or 302 to it with ?redirect=1)."""
    if not microsoft_login.configured():
        raise HTTPException(409, "Microsoft sign-in is not configured on this server")
    client = client if client in _CLIENTS else "web"
    url = microsoft_login.authorize_url(redirect_uri=_redirect_uri(request), client=client)
    if redirect:
        return RedirectResponse(url, status_code=302)
    return {"url": url}


@router.get("/microsoft/callback")
def microsoft_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """OAuth redirect target. Verifies the signed state, exchanges the code, resolves the
    Microsoft identity, find-or-creates the User, mints a session."""
    base = _surplus_base_url(request)
    if error or not code:
        return RedirectResponse(f"{base}/?login=microsoft&status=denied", status_code=302)
    payload = microsoft_login.verify_state(state or "")
    if not payload:
        raise HTTPException(400, "invalid or expired state")
    client = payload.get("c") if payload.get("c") in _CLIENTS else "web"

    tokens = microsoft_login.exchange_code(code=code, redirect_uri=_redirect_uri(request))
    ident = microsoft_login.fetch_identity(tokens.get("access_token", ""))
    if not ident.get("sub"):
        raise HTTPException(400, "Microsoft did not return an identity")

    user = find_or_create_oauth_user(
        db, provider="microsoft", sub=ident["sub"], email=ident["email"],
        name=ident["name"])
    sess = create_session(db, user, client=client)

    if client == "web":
        resp = RedirectResponse(f"{base}/?login=microsoft&status=ok", status_code=302)
        set_session_cookie(resp, sess.session_token, host=request.headers.get("host"))
        return resp
    return JSONResponse({"token": sess.session_token, "client": client})
