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

# Identity of demo users minted by the hidden demo link (routes/demo.py).
# Kept here so both demo.py and /me reference one source of truth without a
# circular import. DEMO_USER_EMAIL is the legacy shared row; per-visitor demo
# users now get demo-<tag>@<DEMO_USER_EMAIL_DOMAIN> so each visit is isolated
# and can never inherit a connected/operator state. is_demo_user() matches both.
DEMO_USER_EMAIL = "demo@surpluslayer.com"
DEMO_USER_EMAIL_DOMAIN = "demo.surpluslayer.com"


def is_demo_user(user) -> bool:
    """True for any demo-link user (legacy shared row or per-visitor mint)."""
    email = (getattr(user, "email", "") or "").lower()
    return email == DEMO_USER_EMAIL or email.endswith(f"@{DEMO_USER_EMAIL_DOMAIN}")


# ─── Emergency outreach kill switch ──────────────────────────────────
# Set SURPLUS_KILL_OUTREACH=1 in Railway env to immediately halt every
# LinkedIn send path without redeploying. Use cases :
#   - Unipile workspace got rate-limited
#   - Someone abuses your Unipile account
#   - You see "wait, that template was wrong" mid-Tech-Week
#
# Surfaced on /api/health.outreach_kill_switch so any operator can see
# at a glance whether the switch is flipped. require_outreach_enabled()
# is called from the per-prospect invite/dm routes and from the batch
# /outreach run path; both turn into 503 with a kill-switch message
# (not 500 — this is intentional + observable).

def kill_switch_engaged() -> bool:
    """True when SURPLUS_KILL_OUTREACH is set to a truthy value."""
    raw = (os.environ.get("SURPLUS_KILL_OUTREACH") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def require_outreach_enabled() -> None:
    """Raises 503 with a kill-switch message when SURPLUS_KILL_OUTREACH is on.
    Per-prospect and batch outreach routes call this before touching any
    provider. Cheap : single env var lookup, no DB."""
    if kill_switch_engaged():
        print("  [outreach.kill_switch] blocked outbound send : "
              "SURPLUS_KILL_OUTREACH is on")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "outreach_kill_switch",
                "message": (
                    "Outreach is temporarily paused by the operator. "
                    "Try again in a few minutes."
                ),
            },
        )

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


def _cookie_domain(host: Optional[str] = None) -> Optional[str]:
    """Domain attribute for our cookies.

    We want ONE login shared across every first-party subdomain (www / apex /
    event.surpluslayer.com). A host-only cookie set during the LinkedIn callback
    on event.surpluslayer.com would otherwise not be re-sent, so the SPA's next
    api.me() 401s and bounces the user back to the login screen even though they
    just authenticated.

    Resolution order:
      1. SESSION_COOKIE_DOMAIN env (explicit operator override; wins).
      2. Auto-derived from the request `host`: any *.surpluslayer.com host (or
         the bare apex) -> ".surpluslayer.com". This is the durable default :
         no env var to forget or mis-set, and it can't lock anyone out because
         it only ever returns a parent domain the request host is already under.
      3. None (host-only) for localhost / *.railway.app / *.fly.dev / IPs, where
         a non-matching Domain would make the browser silently drop the cookie.
    """
    env = (os.environ.get("SESSION_COOKIE_DOMAIN") or "").strip()
    if env:
        return env
    h = (host or "").split(":")[0].strip().lower()
    if h == "surpluslayer.com" or h.endswith(".surpluslayer.com"):
        return ".surpluslayer.com"
    return None


def set_session_cookie(response: Response, session_token: str,
                       host: Optional[str] = None) -> None:
    """Set the surplus session cookie. Lax SameSite so the LinkedIn-hosted-auth
    redirect (a top-level navigation back to our domain) carries the cookie.
    Pass `host` (the request's user-facing host) so the cookie Domain is shared
    across *.surpluslayer.com subdomains even without SESSION_COOKIE_DOMAIN set
    (see _cookie_domain)."""
    secure = _session_cookie_secure()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
        domain=_cookie_domain(host),
    )


def clear_session_cookie(response: Response, host: Optional[str] = None) -> None:
    # Must clear with the SAME Domain it was set with, or the browser keeps the
    # cross-subdomain cookie and logout doesn't actually sign the user out.
    secure = _session_cookie_secure()
    response.delete_cookie(
        key=SESSION_COOKIE, path="/", secure=secure, httponly=True,
        samesite="lax", domain=_cookie_domain(host),
    )


def set_last_account_cookie(response: Response, account_id: str,
                            host: Optional[str] = None) -> None:
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
        domain=_cookie_domain(host),
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
    paid Stripe subscription AND a connected LinkedIn account : Stripe is
    the paywall, the LinkedIn connection is mechanically required to send."""
    return user_has_paid(user) and user_has_linkedin_connected(user)


def require_can_send_linkedin(user: User) -> None:
    """The single gate for every real LinkedIn send : manual one-off
    (invite/dm) AND batch autonomous outreach.

    The whole platform works like the demo : intake, prospecting, scoring,
    matching, ROI, and composing message previews are all free and need no
    payment : signing in and connecting LinkedIn are free too. The one and
    only paywall is here, on the send, and it has two requirements:

      1. A connected, active LinkedIn account (mechanically required to send).
      2. A paid Stripe subscription (the paywall).

    LinkedIn is checked first so a user with neither is asked to connect
    before being asked to pay. The 402 `code` tells the SPA which modal to
    open : `linkedin_send_locked` → connect-LinkedIn, `payment_required` →
    Stripe checkout. A user who has done both sends freely : no paywall.
    """
    if not user_has_linkedin_connected(user):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "linkedin_send_locked",
                "message": (
                    "Connect your LinkedIn account to start sending. We use "
                    "Unipile's hosted auth so the connection stays on your "
                    "LinkedIn account, not ours."
                ),
            },
        )
    if not user_has_paid(user):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "payment_required",
                "message": (
                    "Sending on LinkedIn is a paid feature. Upgrade once and "
                    "your connected LinkedIn account unlocks sending across "
                    "the whole workflow : manual and autonomous."
                ),
            },
        )


# Back-compat aliases : every send gate now means "paid AND connected".
# Kept so existing call sites keep working without churn.
def require_linkedin_connected(user: User) -> None:  # noqa: D401
    """Alias of require_can_send_linkedin."""
    require_can_send_linkedin(user)


def require_paid_auto_outreach(user: User) -> None:  # noqa: D401
    """Alias of require_can_send_linkedin."""
    require_can_send_linkedin(user)


def require_linkedin_send(user: User) -> None:  # noqa: D401
    """Alias of require_can_send_linkedin."""
    require_can_send_linkedin(user)


def require_paid_to_connect_linkedin(user: Optional[User]) -> None:
    """Gate the LinkedIn-connect step on Stripe payment. The product flow is
    pay-first, then LinkedIn : anonymous callers and signed-in unpaid users
    both get bounced to Stripe Checkout before they can connect. The Stripe
    checkout-session endpoint mints an anon User + session for the unsigned
    case, so by the time the caller comes back here post-payment they're
    signed in and paid.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "payment_required",
                "message": (
                    "Upgrade with Stripe to connect LinkedIn. Your payment "
                    "creates your surplus account; the next step links your "
                    "LinkedIn so the agent can send."
                ),
            },
        )
    if user_has_paid(user):
        return
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "payment_required",
            "message": (
                "Upgrade with Stripe to connect LinkedIn. One payment "
                "unlocks LinkedIn sign-in and autonomous outreach across the "
                "whole workflow."
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
