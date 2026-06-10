"""
routes/demo.py : hidden-link demo entry point.

Goal: hand someone (an investor, a friend, a candidate user) a single URL
that drops them into the full surplus app without making them sign in with
their own LinkedIn first. They get a real signed-in session backed by a
dedicated DEMO user, so the entire workflow works end-to-end : intake,
prospecting, fit scoring, matching, ROI, and composing personalized
outreach (preview included).

What's intentionally NOT possible from a demo session: firing real LinkedIn
outreach. The demo user has no connected LinkedIn account (unipile_account_id
is NULL), so every real send route hits the paywall (HTTP 402 via
auth.require_can_send_linkedin) instead of spending anyone's LinkedIn quota or
DMing from a real account. To actually send, the visitor signs in with their
own LinkedIn and upgrades.

Security model:
  - Gated by a shared secret in the DEMO_ACCESS_TOKEN env var.
  - When DEMO_ACCESS_TOKEN is unset, the route returns 404 : it doesn't
    exist in production unless you opt in by setting the env var.
  - constant-time comparison on the token to avoid timing attacks.
  - The blast radius is small now : the worst a leaked link can do is let
    someone poke around the demo workspace. No real sends are possible.

Share URL shape:
  https://www.surpluslayer.com/api/demo/enter?key=<DEMO_ACCESS_TOKEN>

Effect:
  - 303 redirect to "/" with the surplus_session cookie set
  - Each visit mints a fresh demo User row (per-visitor, like
    routes/auth.py:triage_quick_start) so nobody inherits a prior visitor's
    events/prospects OR an accidental LinkedIn connection : every entry is a
    clean, disconnected slate. The dedicated demo email domain lets /me still
    flag is_demo.
  - From that point the SPA behaves like any signed-in but not-LinkedIn-
    connected user : the whole workflow works, sends paywall.

To revoke a leaked link: rotate DEMO_ACCESS_TOKEN in Railway env. Active
sessions issued by the old link continue to work until their 30-day TTL
expires : to kill them immediately, delete the corresponding rows from the
sessions table.
"""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession

from ..auth import DEMO_USER_EMAIL_DOMAIN, create_session, set_session_cookie
from ..db import get_db
from ..models import User


router = APIRouter(prefix="/api/demo", tags=["demo"])

# DEMO_USER_EMAIL_DOMAIN lives in auth.py (single source of truth shared with
# the /me is_demo check). Emails live in our DB only : nothing is ever sent to
# them. unipile_account_id stays NULL so the send capability gate
# (auth.user_can_send_linkedin) treats every demo user as not-connected.

# Stale browser/CDN caches of either the 303 or a 404 from a prior misconfig
# can poison this URL for returning visitors (symptom: regular browser sees
# {"detail":"Not Found"} while incognito works). Set no-store on every
# response so a single bad deploy can never burn the share link.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
}


def _not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"detail": "not found"}, headers=_NO_STORE
    )


def _demo_token() -> Optional[str]:
    """The shared secret. None when the feature is disabled."""
    tok = (os.environ.get("DEMO_ACCESS_TOKEN") or "").strip()
    return tok or None


def _seed_email() -> Optional[str]:
    """Staging-only: the fixed demo email whose workspace every visit reuses.

    When DEMO_SEED_EMAIL is set (we only set it on the staging service), all
    demo entries land in ONE pre-seeded workspace so the populated triage queue
    is visible — see backend/scripts/seed_staging.py. Prod leaves it unset, so
    each visit still mints a fresh, empty, isolated demo user (the secure
    default). Guarded to the demo email domain so a stray value can never point
    the demo link at a real user's account.
    """
    em = (os.environ.get("DEMO_SEED_EMAIL") or "").strip().lower()
    if em and em.endswith(f"@{DEMO_USER_EMAIL_DOMAIN}"):
        return em
    return None


def _mint_demo_user(db: DbSession, *, email: Optional[str] = None) -> User:
    """A fresh, not-LinkedIn-connected demo user.

    Each click on the share link gets its own demo workspace : no events,
    no prospects, nothing carried over, and crucially no inherited LinkedIn
    connection. (A single shared demo row could be turned into an operator
    if anyone ever connected LinkedIn from a demo session, which then leaks
    to every visitor. Per-visitor rows make that impossible.) The dedicated
    demo email domain lets /me still flag is_demo so the SPA hides demo-only
    surfaces like the ROI ledger.

    `email` is supplied only by the staging seed-reuse path (a fixed demo-
    domain address); left None in prod so each visit gets a random isolated tag.
    """
    addr = email or f"demo-{secrets.token_hex(6)}@{DEMO_USER_EMAIL_DOMAIN}"
    user = User(
        name="Surplus Demo",
        email=addr,
        headline="Demo account : full workflow, LinkedIn sending disabled",
        # NULL on purpose : this is what gates real sends behind the paywall.
        unipile_account_id=None,
        linkedin_status="disconnected",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _demo_user_for_visit(db: DbSession) -> User:
    """Pick the demo user for this entry.

    Staging (DEMO_SEED_EMAIL set): reuse the one pre-seeded workspace so the
    visitor sees populated data. Prod (unset): mint a fresh, empty, isolated
    user every visit — the secure default that can never inherit prior state.
    """
    seed = _seed_email()
    if seed:
        existing = db.query(User).filter(User.email == seed).first()
        if existing is not None:
            return existing
        return _mint_demo_user(db, email=seed)
    return _mint_demo_user(db)


# Where a demo visit can land. Keyed allowlist (not raw paths) so the public
# ?surface= value can never be turned into an open redirect : an unknown value
# silently falls back to the desktop pipeline at "/".
_SURFACE_PATHS = {
    "book": "/book",          # advisor "Your book today" surface (BookApp)
    "inperson": "/inperson",  # phone-first capture surface (InPersonApp)
    "app": "/",               # desktop pipeline (default)
}


@router.get("/enter")
def demo_enter(
    key: str = Query(..., description="Shared secret matching DEMO_ACCESS_TOKEN"),
    surface: Optional[str] = Query(
        None, description="Which surface to land on: book | inperson | app (default)"),
    db: DbSession = Depends(get_db),
):
    """Issue a session for the demo user and redirect to the chosen surface.

    `surface` lets a single demo link open straight onto a specific phone
    surface — e.g. ?surface=book drops the visitor on "Your book today".
    Unknown / omitted values fall back to "/" (the desktop pipeline), so the
    existing demo link keeps working unchanged.

    Returns 404 when:
      - DEMO_ACCESS_TOKEN env var is unset (feature disabled)
      - key doesn't match the configured token (don't leak existence)

    Both are 404 (not 403/401) so probing the URL with a wrong key is
    indistinguishable from the feature being off.
    """
    expected = _demo_token()
    if not expected:
        return _not_found()

    # constant-time compare : avoid leaking the token length / prefix via
    # response timing.
    if not hmac.compare_digest(key, expected):
        return _not_found()

    demo_user = _demo_user_for_visit(db)

    target = _SURFACE_PATHS.get((surface or "").strip().lower(), "/")
    sess = create_session(db, demo_user)
    response = RedirectResponse(target, status_code=303)
    for k, v in _NO_STORE.items():
        response.headers[k] = v
    set_session_cookie(response, sess.session_token)
    return response
