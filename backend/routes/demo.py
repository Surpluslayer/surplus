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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession

from ..auth import DEMO_USER_EMAIL_DOMAIN, create_session, is_demo_user, set_session_cookie
from ..db import get_db
from ..demo_seed import build_demo_payload, seed_demo_workspace
from ..hosts import is_first_party, is_inperson_host, request_browser_host
from ..models import Event, Session, User
from ..rate_limit import per_ip_rate_limit


router = APIRouter(prefix="/api/demo", tags=["demo"])

# Anonymous user-creation rate limit for the public /demo door : ~6/min per IP.
# A real visitor opens the walkthrough once; a bot trying to flood the users
# table with demo rows gets blocked. Mirrors routes/auth.py's anonymous limits.
_rl_demo_start = per_ip_rate_limit(limit=6, window_s=60, tag="demo_start")

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
# silently falls back to the per-env default below.
_SURFACE_PATHS = {
    "book": "/book",          # advisor "Your book today" surface (BookApp)
    "inperson": "/inperson",  # phone-first capture surface (InPersonApp)
    "app": "/",               # desktop pipeline
}


def _default_surface() -> str:
    """Which surface a demo link lands on when ?surface= is omitted. Set per
    environment via DEMO_DEFAULT_SURFACE (book | inperson | app) so e.g. the
    staging demo can open straight onto 'Your book today' while production keeps
    the desktop pipeline. Unknown / unset -> "app" (the original behavior)."""
    val = (os.environ.get("DEMO_DEFAULT_SURFACE") or "app").strip().lower()
    return val if val in _SURFACE_PATHS else "app"


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
    When omitted, the landing is the per-environment default (DEMO_DEFAULT_SURFACE,
    "app" if unset) so e.g. staging can default to book while production keeps
    the desktop pipeline. Unknown values fall back to that same default.

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

    surface_key = (surface or "").strip().lower() or _default_surface()
    target = _SURFACE_PATHS.get(surface_key, _SURFACE_PATHS[_default_surface()])
    sess = create_session(db, demo_user)
    response = RedirectResponse(target, status_code=303)
    for k, v in _NO_STORE.items():
        response.headers[k] = v
    set_session_cookie(response, sess.session_token)
    return response


# ─── Public walkthrough door : event.surpluslayer.com/demo ────────────
#
# The token-gated /enter above is for hand-shared private links. The /demo
# walkthrough is the opposite : a PUBLIC, no-key door we want anyone to be able
# to open from a shared URL. It mints a fresh, isolated, LinkedIn-LESS demo user
# (so real sends stay 402-blocked), seeds an in-person workspace + book, and
# returns the script the guided coach-mark tour renders. The visitor converts
# whenever they want by connecting LinkedIn (the persistent banner), which lands
# them on the regular app with onboarding armed.
#
# Gated to the in-person host (event.surpluslayer.com) or a non-first-party dev
# host (localhost / *.railway.app / *.fly.dev preview) so the public door never
# opens on the apex product — exactly like routes/auth.py:inperson_guest.

def _demo_ttl_hours() -> int:
    """Hours a demo workspace lives before it's eligible for cleanup. 0 disables
    cleanup. Env-tunable so staging can keep demos around longer for inspection."""
    try:
        return max(0, int((os.environ.get("DEMO_TTL_HOURS") or "48").strip()))
    except ValueError:
        return 48


def _cleanup_stale_demo_users(db: DbSession) -> None:
    """Best-effort sweep of expired per-visit demo users so the public door
    can't grow the users table without bound. Deletes the demo user's owned
    events (cascade drops their prospects/edges) and sessions, then the user.

    Bounded (a small batch per call) and fully wrapped so a cleanup hiccup can
    never break a fresh visitor's demo start. Only ever touches rows on the
    isolated demo email domain.
    """
    ttl = _demo_ttl_hours()
    if not ttl:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl)
    try:
        stale = (
            db.query(User)
            .filter(User.email.like(f"%@{DEMO_USER_EMAIL_DOMAIN}"))
            .filter(User.last_login_at.isnot(None))
            .filter(User.last_login_at < cutoff)
            .limit(20)
            .all()
        )
        for u in stale:
            if not is_demo_user(u):  # defensive : never touch a real row
                continue
            for ev in db.query(Event).filter(Event.user_id == u.id).all():
                db.delete(ev)  # cascade drops prospects/edges/applicants/sponsors
            db.query(Session).filter(Session.user_id == u.id).delete(
                synchronize_session=False)
            db.delete(u)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"  [demo.cleanup] skipped : {type(exc).__name__}: {exc}")


@router.post("/start", dependencies=[Depends(_rl_demo_start)])
def demo_start(request: Request, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Open the public walkthrough : mint an isolated demo session + seed data,
    return the guided-tour script. 403 on the apex product host."""
    host = request_browser_host(request)
    # Allow the in-person host (event.*) and any non-first-party dev/preview
    # host (localhost, *.railway.app, *.fly.dev). Block the apex product so the
    # public door only exists where the walkthrough is meant to live.
    if is_first_party(host) and not is_inperson_host(host):
        return JSONResponse(
            status_code=403,
            content={"detail": "the demo walkthrough is only available on "
                               "event.surpluslayer.com"},
            headers=_NO_STORE,
        )

    _cleanup_stale_demo_users(db)

    demo_user = _mint_demo_user(db)
    try:
        seed_demo_workspace(db, demo_user)
    except Exception as exc:  # noqa: BLE001
        # The tour is script-driven (payload below), so a seed hiccup must not
        # block the walkthrough : log and continue with an empty workspace.
        db.rollback()
        print(f"  [demo.start] seed failed : {type(exc).__name__}: {exc}")

    sess = create_session(db, demo_user)
    resp = JSONResponse(
        {"ok": True, "user_id": demo_user.id, "demo": build_demo_payload()},
        headers=_NO_STORE,
    )
    set_session_cookie(resp, sess.session_token, host=host)
    return resp
