"""routes/google_login.py : Sign in with Google (the DECOUPLED login).

    GET /api/auth/google/login     -> {url}  (or ?redirect=1 to 302 straight to consent)
    GET /api/auth/google/callback  -> exchange, find-or-create User, mint a session

A brand-new user gets a surplus account + session here WITHOUT any Unipile/LinkedIn
connect -- LinkedIn data is wired up later via the plugin. Web clients get the session
cookie + a redirect into the app; native clients pass ?client=ios|plugin on /login and
the callback returns the Bearer token for them to store (see auth.current_user, which
accepts cookie OR Bearer).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..auth import create_session, set_session_cookie
from ..db import get_db
from ..integrations import google_login
from .auth import _surplus_base_url

router = APIRouter(prefix="/api/auth", tags=["auth"])

_CLIENTS = {"web", "ios", "plugin"}


def _redirect_uri(request: Request) -> str:
    return f"{_surplus_base_url(request)}/api/auth/google/callback"


def find_or_create_google_user(db, *, sub: str, email: str, name: str) -> models.User:
    """One User per person. Match on google_sub; else LINK to an existing same-email
    (non-demo) user -- unifying a Google login with a LinkedIn-first account for the
    same person; else create. Google emails are verified, so the email link is safe."""
    u = None
    if sub:
        u = db.query(models.User).filter(models.User.google_sub == sub).first()
    if u is None and email:
        u = (db.query(models.User)
             .filter(models.User.email == email, models.User.is_demo.is_(False))
             .first())
        if u is not None and not u.google_sub:
            u.google_sub = sub or None
    if u is None:
        u = models.User(google_sub=sub or None, email=email or None, name=name or "")
        db.add(u)
    else:
        if email and not u.email:
            u.email = email
        if name and not u.name:
            u.name = name
    u.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(u)
    return u


@router.get("/google/login")
def google_login_start(request: Request, client: str = "web", redirect: int = 0):
    """Return the Google consent URL (or 302 to it with ?redirect=1). `client` tags the
    session the callback will mint (web cookie vs ios/plugin Bearer)."""
    if not google_login.configured():
        raise HTTPException(409, "Google sign-in is not configured on this server")
    client = client if client in _CLIENTS else "web"
    url = google_login.authorize_url(redirect_uri=_redirect_uri(request), client=client)
    if redirect:
        return RedirectResponse(url, status_code=302)
    return {"url": url}


@router.get("/google/callback")
def google_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """OAuth redirect target. Verifies the signed state, exchanges the code, resolves the
    Google identity, find-or-creates the User, and mints a session. No session cookie
    dependency -- the signed state is the binding."""
    base = _surplus_base_url(request)
    if error or not code:
        return RedirectResponse(f"{base}/?login=google&status=denied", status_code=302)
    payload = google_login.verify_state(state or "")
    if not payload:
        raise HTTPException(400, "invalid or expired state")
    client = payload.get("c") if payload.get("c") in _CLIENTS else "web"

    tokens = google_login.exchange_code(code=code, redirect_uri=_redirect_uri(request))
    ident = google_login.fetch_identity(tokens.get("access_token", ""))
    if not ident.get("sub"):
        raise HTTPException(400, "Google did not return an identity")

    user = find_or_create_google_user(
        db, sub=ident["sub"], email=ident["email"], name=ident["name"])
    sess = create_session(db, user, client=client)

    if client == "web":
        resp = RedirectResponse(f"{base}/?login=google&status=ok", status_code=302)
        set_session_cookie(resp, sess.session_token, host=request.headers.get("host"))
        return resp
    # Native (ios/plugin): hand back the Bearer token for the client to store. (iOS can
    # wrap this in a surplus://auth?token deep-link later; the plugin reads the JSON.)
    return JSONResponse({"token": sess.session_token, "client": client})
