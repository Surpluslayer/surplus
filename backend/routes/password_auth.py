"""routes/password_auth.py : email + password signup / sign-in (the universal path).

    POST /api/auth/signup  {name, email, password, client?}  -> create account + session
    POST /api/auth/login   {email, password, client?}        -> sign in + session

Works for ANY email (Gmail/Outlook/Yahoo/custom), beside the one-tap Google/Microsoft
buttons. Email is the identity, so a password user shares the SAME User row as a
Google/Microsoft login on that address. Web clients get the session cookie; ios/plugin
pass client=ios|plugin and get a Bearer token (auth.current_user accepts either).

Email verification + password reset live in routes/account_email (Resend-backed).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from ..auth import (create_session, hash_password,
                    normalize_client, set_session_cookie, verify_password)
from ..db import get_db
from ..models import User
from ..rate_limit import per_ip_rate_limit

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MIN_PW = 8
_MAX_PW = 128

# Brute-force / abuse guards (per IP).
_rl_signup = per_ip_rate_limit(limit=5, window_s=60, tag="pw_signup")
_rl_login = per_ip_rate_limit(limit=10, window_s=60, tag="pw_login")


class SignupBody(BaseModel):
    name: str
    email: str
    password: str
    client: str = "web"


class LoginBody(BaseModel):
    email: str
    password: str
    client: str = "web"


def _session_response(db: DbSession, user: User, client: str, body: dict,
                      host: str | None = None) -> JSONResponse:
    """Mint a session for `user` and return it the right way for the client: web sets
    the cookie on THIS response (FastAPI gotcha: must be the returned instance); native
    clients get the Bearer token in the JSON to store. `host` (the request's user-facing
    host) is threaded into the cookie so its Domain is shared across *.surpluslayer.com."""
    sess = create_session(db, user, client=client)
    if client == "web":
        resp = JSONResponse(body)
        set_session_cookie(resp, sess.session_token, host=host)
        return resp
    return JSONResponse({**body, "token": sess.session_token, "client": client})


@router.post("/signup", dependencies=[Depends(_rl_signup)])
def signup(body: SignupBody, request: Request, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Create an email+password account. 409 if the email is already registered (via
    password OR an OAuth provider) -- the owner signs in instead; we never attach a
    password to a pre-existing account (that would be takeover)."""
    name = (body.name or "").strip()
    email = (body.email or "").strip().lower()
    pw = body.password or ""
    if not name or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(400, "name and a valid email are required")
    if not (_MIN_PW <= len(pw) <= _MAX_PW):
        raise HTTPException(400, f"password must be {_MIN_PW}-{_MAX_PW} characters")
    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(409, "email already registered (try signing in)")

    user = User(name=name, email=email, password_hash=hash_password(pw))
    db.add(user)
    db.commit()
    db.refresh(user)
    # TOCTOU backstop: users.email has no unique constraint, so two concurrent signups
    # can both insert. Converge on the OLDEST row; losers delete their own insert.
    oldest = (db.query(User).filter(User.email == email)
              .order_by(User.id.asc()).first())
    if oldest is not None and oldest.id != user.id:
        db.delete(user)
        db.commit()
        user = oldest

    # Fire the verification email -- a 6-digit PIN code. `sent` is True only when an
    # email provider is configured AND accepted it. We REQUIRE the code only when it
    # actually went out, so a dormant provider can never lock a new user out.
    from .account_email import send_verification_code
    sent = send_verification_code(db, user)

    return _session_response(db, user, normalize_client(body.client), {
        "ok": True, "user_id": user.id, "name": user.name, "email": user.email,
        "verification_required": bool(sent)}, host=request.headers.get("host"))


@router.post("/login", dependencies=[Depends(_rl_login)])
def login(body: LoginBody, request: Request, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Sign in with email + password. Generic 401 on any failure (no user / no password
    set / wrong password) so we don't reveal which emails exist or use OAuth."""
    email = (body.email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first() if email else None
    if user is None or not verify_password(body.password or "", user.password_hash):
        raise HTTPException(401, "invalid email or password")
    return _session_response(db, user, normalize_client(body.client), {
        "ok": True, "user_id": user.id, "name": user.name, "email": user.email},
        host=request.headers.get("host"))
