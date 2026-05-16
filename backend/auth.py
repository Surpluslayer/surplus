"""
auth.py — session cookie + current_user dependency.

Surplus auth model: Sign in with LinkedIn via Unipile's hosted-auth flow.
There's no separate email/password — the user's LinkedIn account IS their
identity in surplus. See routes/auth.py for the actual flow.

This module owns:
  - Session token generation
  - Cookie read/write
  - current_user / optional_user FastAPI dependencies
"""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session as DbSession

from .db import get_db
from .models import Session, User


SESSION_COOKIE = "surplus_session"
SESSION_TTL_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(db: DbSession, user: User) -> Session:
    """Create + persist a session for `user`. Caller is responsible for
    setting the cookie via set_session_cookie()."""
    sess = Session(
        session_token=_new_session_token(),
        user_id=user.id,
        expires_at=_utcnow() + timedelta(days=SESSION_TTL_DAYS),
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def set_session_cookie(response: Response, session_token: str) -> None:
    """Set the surplus session cookie. Lax SameSite so the LinkedIn-hosted-auth
    redirect (a top-level navigation back to our domain) carries the cookie."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")


def _load_user_by_session(db: DbSession, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    sess = db.query(Session).filter(Session.session_token == token).first()
    if not sess:
        return None
    if sess.revoked_at is not None:
        return None
    if sess.expires_at < _utcnow():
        return None
    sess.last_seen_at = _utcnow()
    db.commit()
    return db.query(User).filter(User.id == sess.user_id).first()


def optional_user(
    db: DbSession = Depends(get_db),
    surplus_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Optional[User]:
    """Returns the signed-in User, or None. Use for routes that can be public
    but behave differently when authed."""
    return _load_user_by_session(db, surplus_session)


def current_user(
    db: DbSession = Depends(get_db),
    surplus_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> User:
    """Returns the signed-in User, or raises 401. Use for protected routes."""
    user = _load_user_by_session(db, surplus_session)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in",
        )
    return user


def revoke_session(db: DbSession, token: str) -> None:
    sess = db.query(Session).filter(Session.session_token == token).first()
    if sess and sess.revoked_at is None:
        sess.revoked_at = _utcnow()
        db.commit()
