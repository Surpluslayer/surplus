"""routes/account_email.py : email verification + password reset.

Stateless tokens (HMAC-signed via integrations.oauth.sign_state, carrying purpose+uid+exp
-- no table). Email goes out via integrations.email_sender (Resend), which is DORMANT
until RESEND_API_KEY is set: the endpoints work, mail just isn't sent until configured.

    POST /api/auth/forgot-password   {email}            -> always 200 (no enumeration)
    POST /api/auth/reset-password    {token, password}  -> set a new password
    POST /api/auth/send-code         (authenticated)    -> (re)send a 6-digit PIN
    POST /api/auth/verify-code       {code}             -> confirm the email via PIN
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

import secrets
from datetime import datetime, timedelta, timezone

from ..auth import current_user, hash_password, verify_password
from ..db import get_db
from ..integrations import email_sender, oauth
from ..models import User
from ..rate_limit import per_ip_rate_limit
from .auth import _surplus_base_url

router = APIRouter(prefix="/api/auth", tags=["auth"])

_TTL_RESET = 60 * 30           # 30m to reset a password
_CODE_TTL = 60 * 10            # 10m for the PIN/OTP email code
_MIN_PW, _MAX_PW = 8, 128

_rl_forgot = per_ip_rate_limit(limit=5, window_s=60, tag="pw_forgot")
_rl_code = per_ip_rate_limit(limit=8, window_s=60, tag="verify_code")  # brute-force guard


class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


class CodeBody(BaseModel):
    code: str


def _gen_code() -> str:
    """A 6-digit numeric PIN (100000-999999)."""
    return str(secrets.randbelow(900000) + 100000)


def send_verification_code(db: DbSession, user: User) -> bool:
    """Generate a 6-digit code, store its bcrypt hash + 10m expiry on the user, and email
    it. Best-effort: returns whether mail actually went out (False = dormant/no email)."""
    if not user.email:
        return False
    code = _gen_code()
    user.email_verify_code_hash = hash_password(code)
    user.email_verify_code_expires = datetime.now(timezone.utc) + timedelta(seconds=_CODE_TTL)
    db.commit()
    return email_sender.send_email(
        to=user.email, subject="Your surplus verification code",
        html=f"<p>Your surplus verification code is:</p>"
             f'<p style="font-size:24px;font-weight:700;letter-spacing:3px">{code}</p>'
             f"<p>It expires in 10 minutes.</p>",
        text=f"Your surplus verification code is {code} (expires in 10 minutes).")


def _sign(purpose: str, uid: int, ttl: int) -> str:
    return oauth.sign_state({"purpose": purpose, "uid": int(uid), "exp": time.time() + ttl})


def _verify(token: str, purpose: str) -> int:
    """Return the uid for a valid token of this purpose, else 0 (exp checked in oauth)."""
    payload = oauth.verify_state(token or "")
    if not payload or payload.get("purpose") != purpose:
        return 0
    return int(payload.get("uid") or 0)


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


@router.post("/send-code")
def send_code(db: DbSession = Depends(get_db),
              user: User = Depends(current_user)) -> JSONResponse:
    """(Re)send the 6-digit email-verification code to the signed-in user. `sent` is
    False when no email provider is configured (dormant) -- the code is still stored."""
    if user.email_verified:
        return JSONResponse({"ok": True, "already_verified": True})
    sent = send_verification_code(db, user)
    return JSONResponse({"ok": True, "sent": sent})


@router.post("/verify-code", dependencies=[Depends(_rl_code)])
def verify_code(body: CodeBody, db: DbSession = Depends(get_db),
                user: User = Depends(current_user)) -> JSONResponse:
    """Confirm the email with the 6-digit code. 400 on missing/expired/wrong code;
    rate-limited so the 6-digit space can't be brute-forced."""
    if user.email_verified:
        return JSONResponse({"ok": True, "already_verified": True})
    expires = user.email_verify_code_expires
    if not user.email_verify_code_hash or expires is None:
        raise HTTPException(400, "no code requested")
    exp = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        raise HTTPException(400, "code expired")
    if not verify_password((body.code or "").strip(), user.email_verify_code_hash):
        raise HTTPException(400, "invalid code")
    user.email_verified = True
    user.email_verify_code_hash = None       # one-time use
    user.email_verify_code_expires = None
    db.commit()
    return JSONResponse({"ok": True})
