"""
auth.py : session cookie + current_user dependency.

Surplus auth model: Sign in with LinkedIn via Unipile's hosted-auth flow.
There's no separate email/password : the user's LinkedIn account IS their
identity in surplus. See routes/auth.py for the actual flow.

This module owns:
  - Session token generation
  - Cookie read/write
  - current_user FastAPI dependency
"""
from __future__ import annotations
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session as DbSession

from .db import get_db
from .models import Session, User


SESSION_COOKIE = "surplus_session"
SESSION_TTL_DAYS = 30

# Identity of the shared demo user minted by the hidden demo link
# (routes/demo.py). Kept here so both demo.py and the /me endpoint can
# reference one source of truth without a circular import.
DEMO_USER_EMAIL = "demo@surpluslayer.com"

# Long-lived cookie remembering which Unipile account this browser was
# last signed in with. Lets /linkedin/start call Unipile's hosted-auth
# with type=reconnect (reuses existing account = no new billed seat)
# instead of type=create on every sign-in.
LAST_ACCOUNT_COOKIE = "surplus_last_account"
LAST_ACCOUNT_TTL_DAYS = 365


def _session_cookie_secure() -> bool:
    """Browsers ignore Set-Cookie with Secure= over plain HTTP, which breaks local
    dev (e.g. Vite at http://localhost). Production uses https:// in
    SURPLUS_BASE_URL, so cookies stay secure unless overridden.

    SURPLUS_SESSION_COOKIE_SECURE: explicit "1"/"true"/"yes" or "0"/"false"/"no".
    If unset, falls back to False when SURPLUS_BASE_URL starts with http://.
    """
    raw = (os.environ.get("SURPLUS_SESSION_COOKIE_SECURE") or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    base = (os.environ.get("SURPLUS_BASE_URL") or "").strip().lower()
    if base.startswith("http://"):
        return False
    return True


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
    secure = _session_cookie_secure()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    secure = _session_cookie_secure()
    response.delete_cookie(
        key=SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax"
    )


def set_last_account_cookie(response: Response, account_id: str) -> None:
    """Persist the Unipile account_id so the next sign-in can use
    type=reconnect. Lax SameSite so the Unipile→callback redirect carries it."""
    secure = _session_cookie_secure()
    response.set_cookie(
        key=LAST_ACCOUNT_COOKIE,
        value=account_id,
        max_age=LAST_ACCOUNT_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns DateTime columns as tz-naive; SQLite returns whatever
    was stored. Coerce both to tz-aware UTC so comparisons with _utcnow() don't
    raise 'can't compare offset-naive and offset-aware datetimes'."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _load_user_by_session(db: DbSession, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    sess = db.query(Session).filter(Session.session_token == token).first()
    if not sess:
        return None
    if sess.revoked_at is not None:
        return None
    expires = _as_aware_utc(sess.expires_at)
    if expires and expires < _utcnow():
        return None
    sess.last_seen_at = _utcnow()
    db.commit()
    return db.query(User).filter(User.id == sess.user_id).first()


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


# ─── Send capability gate ───────────────────────────────────────
# Signing in (current_user) is necessary but not sufficient to fire real
# LinkedIn outreach. Demo and triage-only users get a full-workflow session
# with unipile_account_id=NULL : they can run intake → prospect → match → roi
# and even preview composed messages, but the actual send is a paid feature
# gated behind connecting their own LinkedIn.

def user_has_paid(user: User) -> bool:
    """True when the user has a successful Stripe Checkout on file. The
    paid tier unlocks real LinkedIn sends; free tier can browse, prospect,
    match, and preview composed messages."""
    return getattr(user, "paid_at", None) is not None


def user_has_linkedin_connected(user: User) -> bool:
    """True when the user has connected (and not disconnected) their own
    LinkedIn via Unipile hosted-auth."""
    return (bool(getattr(user, "unipile_account_id", None))
            and user.linkedin_status == "active")


def user_can_send_linkedin(user: User) -> bool:
    """True when `user` may fire real LinkedIn outreach. Requires BOTH a
    paid subscription AND a connected LinkedIn account : payment unlocks
    the feature, the LinkedIn connection is mechanically required to send."""
    return user_has_paid(user) and user_has_linkedin_connected(user)


def require_linkedin_send(user: User) -> None:
    """Gate a real-send route. No-op when the user can send; otherwise raises
    402 with a structured body the SPA renders as the upgrade paywall.

    Two failure codes the frontend branches on:
      - `payment_required`     : free-tier user. Frontend opens Stripe Checkout.
      - `linkedin_send_locked` : paid but no LinkedIn. Frontend opens the
                                  existing "Sign in with LinkedIn" modal.

    Payment is checked FIRST : commercial gate before the LinkedIn one, so
    free users see the price tag before being asked to connect an account.
    """
    if user_can_send_linkedin(user):
        return
    if not user_has_paid(user):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "payment_required",
                "message": (
                    "Real LinkedIn outreach is a paid feature. Upgrade to "
                    "unlock automatic sends across the workflow."
                ),
            },
        )
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "linkedin_send_locked",
            "message": (
                "You're on the paid tier : connect your LinkedIn account to "
                "start sending. We use Unipile's hosted auth so the connection "
                "stays on your LinkedIn account, not ours."
            ),
        },
    )


# ─── Access control ─────────────────────────────────────────────

def get_owned_event(event_id: int, user: User, db: DbSession):
    """Fetch an event by id, requiring `user` to be its owner.

    Returns the Event row. Raises 404 in BOTH the not-found case AND the
    not-owned case : deliberately the same status to avoid leaking the
    existence of other users' events.

    Use from any route handler that takes `event_id` from the URL:

        ev = get_owned_event(event_id, user, db)

    instead of the bare `db.get(Event, event_id)` pattern. After multi-tenant,
    every event-scoped route MUST go through this helper or it leaks data
    across users.
    """
    from .models import Event   # local import to avoid circular at module load
    ev = db.query(Event).filter(Event.id == event_id, Event.user_id == user.id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    return ev
