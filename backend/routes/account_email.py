"""routes/account_email.py : email verification + password reset.

Stateless tokens (HMAC-signed via integrations.oauth.sign_state, carrying purpose+uid+exp
-- no table). Email goes out via integrations.email_sender (Resend), which is DORMANT
until RESEND_API_KEY is set: the endpoints work, mail just isn't sent until configured.

    POST /api/auth/forgot-password   {email}            -> always 200 (no enumeration)
    POST /api/auth/reset-password    {token, password}  -> set a new password
    GET  /api/auth/verify-email?token=...               -> mark email verified, redirect
    POST /api/auth/resend-verification (authenticated)  -> resend the link
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from ..auth import current_user, hash_password
from ..db import get_db
from ..integrations import email_sender, oauth
from ..models import User
from ..rate_limit import per_ip_rate_limit
from .auth import _surplus_base_url

router = APIRouter(prefix="/api/auth", tags=["auth"])

_TTL_VERIFY = 60 * 60 * 24      # 24h to confirm an email
_TTL_RESET = 60 * 30           # 30m to reset a password
_MIN_PW, _MAX_PW = 8, 128

_rl_forgot = per_ip_rate_limit(limit=5, window_s=60, tag="pw_forgot")


class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


def _sign(purpose: str, uid: int, ttl: int) -> str:
    return oauth.sign_state({"purpose": purpose, "uid": int(uid), "exp": time.time() + ttl})


def _verify(token: str, purpose: str) -> int:
    """Return the uid for a valid token of this purpose, else 0 (exp checked in oauth)."""
    payload = oauth.verify_state(token or "")
    if not payload or payload.get("purpose") != purpose:
        return 0
    return int(payload.get("uid") or 0)


def _send_verification(request: Request, user: User) -> None:
    if not user.email:
        return
    token = _sign("verify_email", user.id, _TTL_VERIFY)
    link = f"{_surplus_base_url(request)}/api/auth/verify-email?token={token}"
    email_sender.send_email(
        to=user.email, subject="Confirm your surplus email",
        html=f'<p>Confirm your email to finish setting up surplus:</p>'
             f'<p><a href="{link}">Confirm my email</a></p>'
             f'<p>This link expires in 24 hours.</p>',
        text=f"Confirm your surplus email: {link} (expires in 24h)")


# Called from signup (best-effort; dormant until RESEND_API_KEY is set).
def send_verification_email(request: Request, user: User) -> None:
    try:
        _send_verification(request, user)
    except Exception:  # noqa: BLE001 : email must never break signup
        pass


@router.post("/resend-verification")
def resend_verification(request: Request, user: User = Depends(current_user)) -> JSONResponse:
    if user.email_verified:
        return JSONResponse({"ok": True, "already_verified": True})
    send_verification_email(request, user)
    return JSONResponse({"ok": True, "sent": email_sender.configured()})


@router.get("/verify-email")
def verify_email(token: str, request: Request, db: DbSession = Depends(get_db)):
    base = _surplus_base_url(request)
    uid = _verify(token, "verify_email")
    user = db.get(User, uid) if uid else None
    if user is None:
        return RedirectResponse(f"{base}/?verify=email&status=invalid", status_code=302)
    if not user.email_verified:
        user.email_verified = True
        db.commit()
    return RedirectResponse(f"{base}/?verify=email&status=ok", status_code=302)


@router.post("/forgot-password", dependencies=[Depends(_rl_forgot)])
def forgot_password(body: ForgotBody, request: Request,
                    db: DbSession = Depends(get_db)) -> JSONResponse:
    """Email a reset link IF a password account exists for this address. Always returns
    200 with the same body -- never reveals whether the email exists or uses OAuth."""
    email = (body.email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first() if email else None
    if user is not None and user.password_hash:   # only password accounts can reset
        token = _sign("reset_password", user.id, _TTL_RESET)
        link = f"{_surplus_base_url(request)}/reset-password?token={token}"
        email_sender.send_email(
            to=user.email, subject="Reset your surplus password",
            html=f'<p>Reset your surplus password:</p><p><a href="{link}">Reset password</a></p>'
                 f'<p>This link expires in 30 minutes. If you didn\'t request this, ignore it.</p>',
            text=f"Reset your surplus password: {link} (expires in 30m)")
    return JSONResponse({"ok": True})


@router.post("/reset-password")
def reset_password(body: ResetBody, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Set a new password from a valid reset token. 400 on an invalid/expired token."""
    pw = body.password or ""
    if not (_MIN_PW <= len(pw) <= _MAX_PW):
        raise HTTPException(400, f"password must be {_MIN_PW}-{_MAX_PW} characters")
    uid = _verify(body.token, "reset_password")
    user = db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(400, "invalid or expired reset link")
    user.password_hash = hash_password(pw)
    db.commit()
    return JSONResponse({"ok": True})
