"""
routes/auth.py : Sign in with LinkedIn (via Unipile hosted-auth).

There is no separate email/password layer in surplus. The user's LinkedIn
account IS their identity. The same Unipile connection that auth uses is
the connection we send DMs through later.

Flow
─────
  1. user clicks "Sign in with LinkedIn"
       → frontend POSTs /api/auth/linkedin/start
       → backend creates AuthState(state_token), POSTs to Unipile's
         /hosted/accounts/link with name=state_token, returns {url}
       → frontend window.location = url

  2. user authenticates on Unipile's hosted page (handles 2FA, captcha)

  3. Unipile fires two things, possibly out of order:
       a) webhook → POST /api/auth/linkedin/webhook with {account_id, name}
            we look up state_token, fetch profile, upsert User, mark done
       b) browser redirect → /api/auth/linkedin/callback?state=...
            we look up state_token, create session cookie, redirect to /

  4. subsequent requests carry the surplus_session cookie, current_user
     dependency loads the User row.
"""
from __future__ import annotations
import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from ..auth import (
    LAST_ACCOUNT_COOKIE,
    SESSION_COOKIE,
    _load_user_by_session,
    clear_session_cookie,
    create_session,
    current_user,
    is_demo_user,
    revoke_session,
    set_last_account_cookie,
    set_session_cookie,
)
from .. import billing_plans as bp
from ..db import get_db
from ..hosts import is_first_party, is_inperson_host, request_browser_host
from ..integrations.unipile_config import normalize_unipile_dsn
from ..models import (AuthState, ConnectedAccount, Session, User,
                      list_email_accounts, upsert_email_account)
from ..rate_limit import per_ip_rate_limit


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _arm_onboarding_if_first_connect(user: User) -> None:
    """Arm the in-product onboarding tour the instant a user FIRST gains a
    LinkedIn connection.

    Called from both the webhook and the callback upsert paths, after `user`
    has been assigned the connecting account. Gated on the empty default of
    onboarding_status so it fires exactly once per user : a brand-new signup
    or a triage-only user connecting LinkedIn for the first time gets armed;
    a re-connect / profile refresh (status already 'active'/'done'/'skipped')
    is a no-op. The actual coachmarks render on the in-person surface, which
    reads this off /me."""
    if not (getattr(user, "onboarding_status", "") or ""):
        user.onboarding_status = "active"
        user.onboarding_step = 0


def _seed_conversations_and_voice(db, user_id: int) -> None:
    """Detached seed body (run via jobs.run_detached): import the user's genuine
    LinkedIn DM conversations into the Book, then learn their voice. Top-level so
    the Modal durable path can resolve it. `db` is the run_detached-owned session
    used for the import; the voice sync opens its OWN second session (the import
    may have committed/closed work on `db`). Idempotent + best-effort.

    Session lifecycle: import_conversation_contacts releases db's pooled
    connection before its minutes-long Unipile walk (see its docstring), so
    this job never pins a connection across network I/O. The voice sync's
    single bounded fetch (8 sent messages) is the only network done while its
    own short session is open."""
    from ..agents.relationship.spine.relationships import import_conversation_contacts
    from ..models import User as _User
    try:
        u = db.get(_User, user_id)
        if u is not None:
            res = import_conversation_contacts(db, u, want=15)
            print(f"[autoimport] user={user_id} {res}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[autoimport] user={user_id} failed: {type(exc).__name__}: {exc}",
              flush=True)
    # Learn the host's voice from their own sent messages, in its own session.
    # Same ban-safe own-account read surface; idempotent; never blocks the connect.
    from ..db import SessionLocal
    vdb = SessionLocal()
    try:
        from ..agents.live_enrich import sync_host_voice_on_connect
        vres = sync_host_voice_on_connect(vdb, user_id)
        print(f"[voicesync] user={user_id} {vres}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[voicesync] user={user_id} failed: {type(exc).__name__}: {exc}",
              flush=True)
    finally:
        vdb.close()


def _autoimport_conversations(user_id: int) -> None:
    """Background: seed the Book from the user's genuine LinkedIn DM conversations
    right after they connect, so the spine isn't empty. DURABLE (prefer_modal) so
    a deploy mid-seed doesn't drop it. Idempotent + best-effort; never blocks or
    fails the auth response.

    Two complementary passes, both idempotent:
      1. the conversation seed (contacts + voice from the most active chats), and
      2. the FULL LinkedIn chat sync (message bodies into each contact's
         timeline, the context the drafter reads) -- same magic-moment pattern
         as the WhatsApp first sync: connect -> the book fills itself, no
         waiting for the 6h gathering sweep."""
    from ..jobs import run_detached
    run_detached(_seed_conversations_and_voice, user_id, prefer_modal=True)
    try:
        from ..agents.relationship.linkedin_chat_sync import dispatch_linkedin_chat_sync
        runner = dispatch_linkedin_chat_sync(user_id, incremental=False)
        print(f"  [auth.linkedin] dispatched first chat sync user.id={user_id} "
              f"runner={runner}")
    except Exception as exc:  # noqa: BLE001 -- seeding must never fail the connect
        print(f"  [auth.linkedin] first chat sync dispatch failed "
              f"user.id={user_id}: {type(exc).__name__}: {exc}")


def _email_first_sync(db, user_id: int) -> None:
    """Detached email first-sync body (run via jobs.run_detached). Top-level so
    the Modal durable path can resolve it; `db` is the run_detached-owned session.
    Re-derives Unipile creds from env (the durable Modal worker has its own env).
    Best-effort; idempotent (sync skips by message id)."""
    from ..agents.relationship.email_sync import sync_email_contacts
    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    if not (dsn and api_key):
        print(f"  [auth.email] first sync user.id={user_id}: unipile not configured")
        return
    u = db.query(User).filter(User.id == user_id).first()
    if u is not None:
        stats = sync_email_contacts(db, u, dsn=dsn, api_key=api_key)
        print(f"  [auth.email] first sync user.id={user_id}: {stats}")

# Anonymous user-creation rate limit : ~5/min per IP. A real Tech Week
# demo viewer clicking around does ~1/min ; a bot trying to fill up the
# users table gets blocked at 6/min. Also applied to triage signup +
# checkout-session (other anonymous routes that create users).
_rl_triage_signup = per_ip_rate_limit(limit=5, window_s=60, tag="triage_signup")
_rl_triage_signup_email = per_ip_rate_limit(limit=10, window_s=60, tag="triage_signup_email")


# ─── Unipile config + HTTP helpers ─────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _unipile_dsn() -> Optional[str]:
    return normalize_unipile_dsn(os.environ.get("UNIPILE_DSN")) or None


def _unipile_api_key() -> Optional[str]:
    return (os.environ.get("UNIPILE_API_KEY", "") or "").strip() or None


# Production origin hosts that sit behind www.surpluslayer.com via the
# Cloudflare load balancer. When a request arrives at one of these via
# the CDN, Railway/Fly's edge rewrites the Host header to the origin's
# own hostname — so request.url.netloc is misleading. We hardcode the
# apex as the user-facing URL for these hosts so Unipile's success_
# redirect_url + notify_url always point at the apex, regardless of
# which backend the LB happened to pick or whether SURPLUS_BASE_URL is
# set in the environment.
_PRODUCTION_APEX = "https://www.surpluslayer.com"
_PRODUCTION_ORIGIN_HOSTS = (
    "surplus-production.up.railway.app",
    "surplus-prod.fly.dev",
    "surplus.fly.dev",
)


def _surplus_base_url(request: Request) -> str:
    """Base URL the user's browser sees us at : used to construct redirect/notify
    URLs Unipile will call back. Resolution order:

      1. SURPLUS_BASE_URL env (explicit operator override; wins everything)
      2. Hardcoded apex when the request's Host is a known production
         origin behind the CDN (belt-and-suspenders against missing env
         var on Railway/Fly)
      3. The request's own origin — ONLY when that origin is trusted
         (a *.surpluslayer.com / *.railway.app / localhost host). A forged
         Host is never echoed back (see SECURITY note below).

    Always force https:// for surpluslayer.com / railway.app hosts :
    Railway terminates SSL upstream so request.url.scheme is "http" but
    the user-facing URL is "https"."""
    env = (os.environ.get("SURPLUS_BASE_URL", "") or "").strip().rstrip("/")
    if env:
        return env
    host = request.url.netloc
    if any(host == h or host.startswith(h + ":") for h in _PRODUCTION_ORIGIN_HOSTS):
        return _PRODUCTION_APEX
    # SECURITY: never echo an untrusted Host header back into a user-facing URL.
    # This value builds emailed password-reset links and OAuth success redirects;
    # a forged `Host: evil.com` (when SURPLUS_BASE_URL is unset) would otherwise
    # send the reset TOKEN to an attacker host = account takeover. Only trust the
    # request host when it is genuinely one of ours; anything else falls back to
    # the production apex, not the attacker value.
    from ..hosts import is_first_party
    bare_host = host.split(":")[0].lower()
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    scheme = forwarded or request.url.scheme
    if is_first_party(bare_host):
        return f"https://{host}"
    if "railway.app" in bare_host or bare_host in ("localhost", "127.0.0.1") \
            or bare_host.startswith("127."):
        return f"{scheme}://{host}"
    return _PRODUCTION_APEX


def _redirect_base(request: Request) -> str:
    """Base URL the LinkedIn flow should return the user to.

    Same as _surplus_base_url, EXCEPT: when the flow began on the in-person
    host (event.surpluslayer.com), keep the success/failure redirects on that
    host so the user stays on the in-person surface end-to-end instead of being
    dropped on the apex and having to re-find their way back. Guarded to
    first-party hosts so a forged Origin can't turn this into an open redirect.
    """
    from ..hosts import request_browser_host, is_inperson_host, is_first_party
    host = request_browser_host(request)
    if host and is_first_party(host) and is_inperson_host(host):
        return f"https://{host}"
    return _surplus_base_url(request)


def _unipile_iso_timestamp(dt: datetime) -> str:
    """Format a UTC datetime as Unipile's strict ISO 8601 with exactly 3-digit ms.
    Unipile's regex is ^[1-2]\\d{3}-[0-1]\\d-[0-3]\\dT\\d{2}:\\d{2}:\\d{2}.\\d{3}Z$
    so Python's default isoformat (microseconds, +00:00) is rejected.
    """
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def _ensure_unipile_configured() -> tuple[str, str]:
    dsn = _unipile_dsn()
    api_key = _unipile_api_key()
    if not (dsn and api_key):
        raise HTTPException(
            status_code=503,
            detail="LinkedIn auth is not configured: UNIPILE_DSN + UNIPILE_API_KEY required",
        )
    return dsn, api_key


# ─── 1. Start: create hosted-auth link ─────────────────────────────

def _create_body(dsn: str, expires: str, state_token: str, base: str,
                 failure_url: str) -> dict:
    """Full create body : providers, notify_url, name, both redirects."""
    return {
        "type": "create",
        "providers": ["LINKEDIN"],
        "api_url": dsn,
        "expiresOn": expires,
        "success_redirect_url": f"{base}/api/auth/linkedin/callback?state={state_token}",
        "failure_redirect_url": failure_url,
        "notify_url": f"{base}/api/auth/linkedin/webhook",
        "name": state_token,
    }


def _reconnect_body(dsn: str, expires: str, state_token: str, base: str,
                    failure_url: str, account_id: str) -> dict:
    """Minimal reconnect body per Unipile docs : extra create-only fields
    (providers / notify_url / name) cause Unipile to 4xx with
    'linkedin_unipile_rejected'. Keep only what the docs example shows
    plus the redirect URLs (so the browser comes back to us)."""
    return {
        "type": "reconnect",
        "reconnect_account": account_id,
        "api_url": dsn,
        "expiresOn": expires,
        "success_redirect_url": f"{base}/api/auth/linkedin/callback?state={state_token}",
        "failure_redirect_url": failure_url,
    }


def _resolve_returning_user(request: Request, db: DbSession) -> Optional[User]:
    """Find the returning user so we reconnect their existing Unipile account
    (and re-point it onto the same User row) instead of minting a brand-new
    account + duplicate User that orphans their events.

    Resolution order:
      1. LAST_ACCOUNT_COOKIE -> User.unipile_account_id : the same-browser
         marker (set on every successful auth, 365-day TTL).
      2. SESSION_COOKIE -> the currently-logged-in User, IF they already
         have a live unipile_account_id to reconnect. This is the fix for
         re-auth from a browser that lost the LAST_ACCOUNT_COOKIE (different
         device, cleared cookies, cookie expiry, or the prior account was
         deleted upstream). Without it, a logged-in operator re-connecting
         looked like a brand-new caller -> create -> orphaned events.

    Returns None only for genuinely new/anonymous callers (no cookie, no
    session, or a session user who has never connected LinkedIn) : caller
    then falls back to create."""
    last_account = (request.cookies.get(LAST_ACCOUNT_COOKIE) or "").strip()
    if last_account:
        by_cookie = db.query(User).filter(
            User.unipile_account_id == last_account).first()
        if by_cookie is not None:
            return by_cookie

    # Fallback : a logged-in user re-connecting without a usable
    # LAST_ACCOUNT_COOKIE. Only reconnect if they already hold a live
    # account_id; a triage / email-only user with no Unipile account must
    # still go through create (reconnect needs an account to reconnect to).
    session_user = _load_user_by_session(
        db, (request.cookies.get(SESSION_COOKIE) or "").strip() or None
    )
    if session_user is not None and session_user.unipile_account_id:
        return session_user
    return None


async def _post_hosted_link(dsn: str, api_key: str, body: dict) -> tuple[int, dict]:
    """POST the body to Unipile, returning (status_code, response_json).
    Caller decides how to handle 4xx vs success."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{dsn}/api/v1/hosted/accounts/link",
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            json=body,
        )
    try:
        data = r.json() if r.content else {}
    except Exception:
        data = {"_raw": r.text[:500]}
    return r.status_code, data


# ─── Native app (iOS Capacitor) sign-in support ─────────────────────
# The iOS app is a WebView; LinkedIn login can't complete inside an embedded
# WebView, and a cookie set during an external-browser login lands in the wrong
# cookie jar. So the app runs sign-in in the SYSTEM browser and we hand the
# session back via a deep link: the callback redirects to
# surplus://auth?token=<session_token> instead of the web app. We flag the flow
# by PREFIXING the state token (round-trips through AuthState/Unipile unchanged),
# so no schema change is needed. The app then opens
# /api/auth/mobile-adopt?token=... inside its WebView, which sets the
# surplus_session cookie there — so the whole web app is authenticated.
MOBILE_STATE_PREFIX = "m_"
MOBILE_REDIRECT_URI = "surplus://auth"  # must match CFBundleURLSchemes in the app


def _is_mobile_state(state_token: Optional[str]) -> bool:
    return bool(state_token) and state_token.startswith(MOBILE_STATE_PREFIX)


def _is_cross_site_toplevel_nav(request: Request) -> bool:
    """True when a GET is a CROSS-SITE TOP-LEVEL navigation — the classic
    login-CSRF / session-fixation vector for the cookie-adopting endpoints below.

    An attacker lures a victim to click a link that sets the victim's session
    cookie to an ATTACKER-supplied token, silently placing the victim inside the
    attacker's account. We detect it via the browser's Fetch-Metadata headers:
      - Sec-Fetch-Site: cross-site  → navigation initiated by another site/email
      - Sec-Fetch-Dest: document    → a TOP-LEVEL page load (the link click)

    The legitimate callers are NOT cross-site top-level documents:
      - the extension loads /token-bootstrap in an IFRAME (Dest: iframe),
      - the native app opens /mobile-adopt from a surplus:// deep link
        (Site: none, OS-initiated),
      - same-origin SPA navigations are Site: same-origin / same-site.

    Browsers without Fetch-Metadata omit these headers; we fail OPEN there (this
    is defense-in-depth layered on the token check), blocking only when the
    headers positively identify a cross-site top-level navigation."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    dest = (request.headers.get("sec-fetch-dest") or "").strip().lower()
    return site == "cross-site" and dest == "document"


@router.get("/mobile-adopt", include_in_schema=False)
def mobile_adopt(
    request: Request,
    token: str = Query(...),
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    """Adopt a session token handed to the native app via the surplus://auth
    deep link. Validates the token against a live session, then sets the
    surplus_session cookie on THIS WebView navigation and redirects into the
    app. Because the token IS the session secret, holding it already grants the
    session — this just moves it into the WebView's cookie jar."""
    # SECURITY: refuse to set a session cookie on a cross-site top-level click
    # (login-CSRF / session fixation). The real WebView deep-link is Site:none.
    if _is_cross_site_toplevel_nav(request):
        return RedirectResponse("/signin?error=mobile_adopt_blocked", status_code=303)
    tok = (token or "").strip()
    _h = request_browser_host(request) or None
    ok = False
    if tok:
        sess = db.query(Session).filter(Session.session_token == tok).first()
        ok = bool(sess and sess.revoked_at is None)
    if not ok:
        return RedirectResponse("/signin?error=mobile_adopt_failed", status_code=303)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, tok, host=_h)
    return response


@router.post("/linkedin/start")
async def linkedin_start(
    request: Request,
    db: DbSession = Depends(get_db),
    mobile: bool = Query(
        False,
        description="Set by the native app so the callback deep-links the "
        "session token back to the app instead of redirecting the web SPA.",
    ),
) -> JSONResponse:
    dsn, api_key = _ensure_unipile_configured()

    # Prefix the state token for the native app so the callback recognises the
    # flow origin without a schema change (see MOBILE_STATE_PREFIX).
    state_token = secrets.token_urlsafe(32)
    if mobile:
        state_token = MOBILE_STATE_PREFIX + state_token
    auth_state = AuthState(state_token=state_token, status="pending")
    db.add(auth_state)
    db.flush()  # populate auth_state.id without committing yet

    base = _redirect_base(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/signin?error=linkedin_auth_failed"

    # The LinkedIn round-trip ALWAYS proceeds : the pay-at-connect paywall is
    # enforced in /linkedin/callback, AFTER we know the LinkedIn identity, so
    # payment is tied to the LinkedIn account (provider_id) and portable across
    # browsers/devices. Gating here (before identity is known) would wrongly
    # bounce a PAID LinkedIn signing in from a fresh browser to Stripe again.
    active_user = _load_user_by_session(
        db, (request.cookies.get(SESSION_COOKIE) or "").strip() or None
    )

    # Pre-tag the AuthState with the signed-in user's id when we have one, so
    # the callback merges the LinkedIn fields into the existing row (preserving
    # paid_at / session) instead of creating a duplicate. Anonymous callers
    # leave it None : the callback upserts by LinkedIn identifiers / email.
    #
    # Demo users are the exception : a /demo visitor signing in must get a
    # FRESH real account, never adopt the throwaway demo row (which would drag
    # the seeded demo workspace into their real account). Leaving user_id None
    # routes them through the normal dedup/create path.
    if active_user is not None and not is_demo_user(active_user):
        auth_state.user_id = active_user.id

    returning = _resolve_returning_user(request, db)

    # Same-browser returning user? Use reconnect (reuses their Unipile
    # account, no new seat). Pre-fill AuthState.user_id so the callback
    # doesn't need to wait for a webhook to correlate by state_token :
    # the reconnect body strips `name`, so the webhook can't tag the
    # state itself anyway.
    if returning is not None:
        auth_state.user_id = returning.id
        db.commit()
        body = _reconnect_body(dsn, expires, state_token, base,
                               failure_url, returning.unipile_account_id)
    else:
        db.commit()
        body = _create_body(dsn, expires, state_token, base, failure_url)
    try:
        status, data = await _post_hosted_link(dsn, api_key, body)
        # Reconnect can legitimately fail (account deleted on Unipile side,
        # API change, etc.) : fall back to create so the user isn't locked out.
        if status >= 400 and body["type"] == "reconnect":
            print(f"  [auth] reconnect rejected ({status}); falling back to create")
            # Keep auth_state.user_id pointing at the returning user. Reconnect
            # most often fails because the OLD account was deleted upstream :
            # exactly the case where we must re-point the existing User row
            # onto the new account. The callback/webhook adopt-pre-tagged
            # branch does that (and backfills the now-known provider_id),
            # which is what stops the duplicate-User orphaning. Clearing it
            # here was the bug : it forced dedup-by-keys, which misses when
            # the old row's linkedin_provider_id is NULL.
            body = _create_body(dsn, expires, state_token, base, failure_url)
            status, data = await _post_hosted_link(dsn, api_key, body)
        if status >= 400:
            detail = (data.get("message") or data.get("detail")
                      or data.get("error") or data or f"HTTP {status}")
            raise HTTPException(status_code=502, detail={
                "where": "unipile /hosted/accounts/link",
                "status": status, "unipile_response": detail,
                "request_dsn": dsn, "request_body_type": body["type"],
            })
        return JSONResponse({"url": data.get("url"), "state_token": state_token})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Unipile: {e!r}")


# ─── 1b. Connect-first start (for landing-page links) ─────────────
# The POST /linkedin/start returns JSON but is paywalled. The landing
# page (join.surpluslayer.com) is connect-first/free, so it uses the
# helper below via two thin endpoints:
#   - start-redirect : top-level GET navigation, 303s straight to Unipile
#     so the cookie set on /linkedin/callback arrives in the same chain.
#   - start-url      : same minting, returns JSON {"url": ...} so the
#     landing can PREFETCH the hosted link on hover/focus and make the
#     actual click instant (the ~1s Unipile round-trip runs ahead of time).

async def _mint_connect_link(
    request: Request, db: DbSession
) -> tuple[str | None, str | None]:
    """Connect-first (free) mint of a Unipile hosted-auth link.

    Returns (url, None) on success or (None, error_key) on failure, where
    error_key is one of the ?error= values the landing page understands.
    """
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    auth_state = AuthState(state_token=state_token, status="pending")
    db.add(auth_state)
    db.flush()

    base = _redirect_base(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/?error=linkedin_auth_failed"

    # Connect-first : connecting LinkedIn is free (paywall is at SEND). An
    # anonymous caller just starts the flow; the callback mints the User.
    active_user = _load_user_by_session(
        db, (request.cookies.get(SESSION_COOKIE) or "").strip() or None
    )
    # Pre-tag so the callback merges LinkedIn fields into the existing row
    # instead of orphaning it. See linkedin_start() for the rationale. Demo
    # users are skipped so a /demo → connect flow mints a fresh real account
    # instead of adopting the throwaway demo row (and its seed data).
    if active_user is not None and not is_demo_user(active_user):
        auth_state.user_id = active_user.id

    returning = _resolve_returning_user(request, db)
    if returning is not None:
        auth_state.user_id = returning.id
        db.commit()
        body = _reconnect_body(dsn, expires, state_token, base,
                               failure_url, returning.unipile_account_id)
    else:
        db.commit()
        body = _create_body(dsn, expires, state_token, base, failure_url)
    try:
        status, data = await _post_hosted_link(dsn, api_key, body)
        if (status >= 400 or not data.get("url")) and body["type"] == "reconnect":
            print(f"  [auth] reconnect rejected ({status}); falling back to create")
            # Keep the pre-tag : see linkedin_start() : the callback adopts
            # the returning User row onto the new account instead of minting
            # a duplicate that orphans events.
            body = _create_body(dsn, expires, state_token, base, failure_url)
            status, data = await _post_hosted_link(dsn, api_key, body)
        if status >= 400 or not data.get("url"):
            return None, "linkedin_unipile_rejected"
        return data["url"], None
    except httpx.HTTPError:
        return None, "linkedin_unreachable"


@router.get("/linkedin/start-redirect")
async def linkedin_start_redirect(
    request: Request,
    db: DbSession = Depends(get_db),
):
    url, error = await _mint_connect_link(request, db)
    if url is None:
        base = _redirect_base(request)
        return RedirectResponse(f"{base}/?error={error}", status_code=303)
    return RedirectResponse(url, status_code=303)


@router.get("/linkedin/start-url")
async def linkedin_start_url(
    request: Request,
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    """JSON sibling of start-redirect for prefetch. The landing page calls
    this on hover/focus, caches the url, then navigates to it on click so
    the Unipile round-trip is already done. CORS is open (no credentials),
    so the cross-origin fetch from join.surpluslayer.com just works."""
    url, error = await _mint_connect_link(request, db)
    if url is None:
        return JSONResponse({"error": error}, status_code=502)
    return JSONResponse({"url": url})


# ─── 2. Webhook: Unipile tells us a new account was created ────────

async def _fetch_unipile_profile(account_id: str, dsn: str, api_key: str) -> dict:
    """Pull the connected LinkedIn profile so we can populate the User row
    with name + avatar + email. Best-effort : returns {} on failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{dsn}/api/v1/accounts/{account_id}",
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
            )
            if r.status_code >= 400:
                return {}
            return r.json() or {}
    except Exception:
        return {}


async def _delete_unipile_account(
    account_id: str, dsn: str, api_key: str
) -> bool:
    """Remove an orphan Unipile account that the dedup logic detected was
    a duplicate. Called after we've migrated our User row to the new
    account_id, so this account is no longer needed.

    Best-effort : a failure here just leaves the orphan in Unipile's
    dashboard (manual cleanup) but doesn't break sign-in. Logs loudly
    so we can spot patterns if the delete API breaks.
    """
    if not account_id or not dsn or not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.delete(
                f"{dsn}/api/v1/accounts/{account_id}",
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [auth.dedup.delete] account={account_id} "
              f"transport_error={type(exc).__name__}: {exc}")
        return False
    if r.status_code >= 400:
        print(f"  [auth.dedup.delete] account={account_id} "
              f"HTTP {r.status_code} body={r.text[:160]}")
        return False
    print(f"  [auth.dedup.delete] account={account_id} deleted from Unipile")
    return True


def _extract_profile_fields(account_data: dict) -> dict:
    """Pluck the fields we want out of Unipile's account payload, tolerating
    the camelCase keys Unipile actually returns plus snake_case variants
    seen in older docs / other providers.

    The dedup loop in linkedin_callback / linkedin_webhook depends on
    linkedin_public_id + linkedin_provider_id being populated. Before this
    fix the extractor looked at `public_identifier` / `entity_urn` while
    Unipile actually returns `publicIdentifier` / `id` under `connection_params.im`,
    so every existing User row had NULL dedup keys and a fresh sign-in
    couldn't match itself to the existing row. That's the source of the
    duplicate-Unipile-accounts-per-person issue in the dashboard.
    """
    params = account_data.get("connection_params") or account_data.get("params") or {}
    li = params.get("im") or params.get("linkedin") or params  # forgive variations

    name = (
        account_data.get("name")
        or li.get("name")
        or li.get("username")
        or " ".join(filter(None, [li.get("first_name"), li.get("last_name")]))
        or ""
    ).strip()
    out = {
        "name": name,
        "email": account_data.get("email") or li.get("email"),
        "headline": li.get("headline") or li.get("occupation"),
        "avatar_url": (
            account_data.get("picture")
            or li.get("picture_url")
            or li.get("picture")
            or li.get("profile_picture_url")
            or li.get("pictureUrl")
        ),
        # Unipile returns camelCase: `publicIdentifier`. Older docs / other
        # providers use `public_identifier` / `vanityName`.
        "linkedin_public_id": (
            li.get("publicIdentifier")
            or li.get("public_identifier")
            or li.get("vanityName")
        ),
        # Unipile returns the LinkedIn URN directly as `id` (e.g. ACoAA...).
        # Older shapes used `entity_urn` / `provider_id` / `member_urn`.
        "linkedin_provider_id": (
            li.get("id")
            or li.get("entity_urn")
            or li.get("provider_id")
            or li.get("member_urn")
        ),
    }
    # Loud, observable warning if Unipile's shape drifts again. Without this
    # the dedup keys silently went NULL for every user.
    if account_data and not (out["linkedin_provider_id"] or out["linkedin_public_id"]):
        print(
            "  [auth.extract] WARNING : no linkedin_provider_id or "
            "linkedin_public_id extracted. Unipile payload shape may have "
            f"changed. account_data keys={list(account_data.keys())} "
            f"im keys={list(li.keys()) if isinstance(li, dict) else 'n/a'}"
        )
    return out


@router.post("/linkedin/webhook")
async def linkedin_webhook(payload: dict, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Unipile posts here when a hosted-auth account is created or fails.

    Expected shape (per Unipile docs):
      { "status": "CREATION_SUCCESS" | "CREATION_FAILED",
        "account_id": "...", "name": "<our state_token>" }
    """
    status_raw = (payload.get("status") or "").upper()
    state_token = (payload.get("name") or "").strip()
    account_id = (payload.get("account_id") or "").strip()

    if not state_token:
        # Not from a hosted-auth flow we initiated; ignore but ack so Unipile
        # doesn't retry.
        return JSONResponse({"ok": True, "ignored": "no state_token"})

    auth_state = db.query(AuthState).filter(AuthState.state_token == state_token).first()
    if not auth_state:
        return JSONResponse({"ok": True, "ignored": "unknown state_token"})

    if status_raw not in {"CREATION_SUCCESS", "RECONNECTED"} or not account_id:
        auth_state.status = "failed"
        auth_state.error = f"unipile status={status_raw}"
        auth_state.completed_at = _utcnow()
        db.commit()
        return JSONResponse({"ok": True, "recorded": "failure"})

    # Pull profile, upsert User by unipile_account_id
    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    profile = await _fetch_unipile_profile(account_id, dsn, api_key) if (dsn and api_key) else {}
    fields = _extract_profile_fields(profile)

    user = db.query(User).filter(User.unipile_account_id == account_id).first()
    now = _utcnow()
    orphan_unipile_account_id: Optional[str] = None  # captured for post-commit delete
    if user is None and auth_state.user_id is not None:
        # Stripe-first signup : /linkedin/start pre-tagged the AuthState with
        # the paid prepay user's id. Adopt that row so LinkedIn fields merge
        # into the same User (preserving paid_at) instead of creating a
        # duplicate. Without this, name stays "Surplus user" after sign-in.
        user = db.query(User).filter(User.id == auth_state.user_id).first()
        if user is not None:
            print(f"  [auth.webhook] adopting pre-tagged user.id={user.id} "
                  f"for new unipile_account_id={account_id}")
            if user.unipile_account_id and user.unipile_account_id != account_id:
                orphan_unipile_account_id = user.unipile_account_id
            user.unipile_account_id = account_id
    if user is None:
        # Same dedup as the URL-callback path : if this account_id is new
        # but the LinkedIn person isn't, claim the existing User row and
        # capture the old account_id so we can delete the orphan from
        # Unipile after commit. Without this, Unipile-webhook-first flows
        # (the common case) bypass dedup entirely and leak duplicates.
        for key, val in (
            ("linkedin_provider_id", fields.get("linkedin_provider_id")),
            ("linkedin_public_id", fields.get("linkedin_public_id")),
            ("email", fields.get("email")),
        ):
            if not val:
                continue
            user = db.query(User).filter(getattr(User, key) == val).first()
            if user is not None:
                print(f"  [auth.dedup] (webhook) claimed existing user.id={user.id} "
                      f"via {key} : new unipile_account_id={account_id}")
                if user.unipile_account_id and user.unipile_account_id != account_id:
                    orphan_unipile_account_id = user.unipile_account_id
                user.unipile_account_id = account_id
                break
    if user:
        # Existing user re-connecting : refresh profile fields, mark active
        for k, v in fields.items():
            if v:
                setattr(user, k, v)
        user.last_login_at = now
        user.linkedin_status = "active"
    else:
        user = User(
            unipile_account_id=account_id,
            name=fields.get("name") or "LinkedIn user",
            email=fields.get("email"),
            headline=fields.get("headline"),
            avatar_url=fields.get("avatar_url"),
            linkedin_public_id=fields.get("linkedin_public_id"),
            linkedin_provider_id=fields.get("linkedin_provider_id"),
            last_login_at=now,
            linkedin_status="active",
        )
        db.add(user)
        db.flush()  # need user.id

    # First LinkedIn connection -> arm the onboarding tour (once per user).
    _arm_onboarding_if_first_connect(user)

    auth_state.user_id = user.id
    auth_state.status = "webhook_done"
    auth_state.completed_at = now
    db.commit()

    # Seed the Book from their genuine DM conversations (backgrounded, idempotent).
    _autoimport_conversations(user.id)

    # Same orphan-delete as the URL-callback path : after commit, drop the
    # old Unipile account from Unipile's dashboard so they don't bill us
    # for duplicate seats. Best-effort.
    if orphan_unipile_account_id and dsn and api_key:
        await _delete_unipile_account(orphan_unipile_account_id,
                                      dsn, api_key)

    return JSONResponse({"ok": True, "user_id": user.id})


# ─── 3. Callback: user lands here after Unipile auth ───────────────

@router.get("/linkedin/callback")
async def linkedin_callback(
    request: Request,
    state: str = Query(...),
    account_id: Optional[str] = Query(None),
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    """User's browser redirected here by Unipile after they auth'd.

    Two information sources arrive here in parallel :
      1. Webhook (POST /linkedin/webhook) — fires from Unipile to us with
         the full payload (status, account_id, name=state_token). When
         the payload has `name`, the webhook handler links AuthState to
         User. Some Unipile configurations don't echo `name` back, in
         which case the webhook 200s but no-ops.
      2. Callback redirect (this endpoint) — Unipile appends
         `?account_id=<id>` to the URL we registered as success_redirect.

    Strategy : if AuthState already has user_id (webhook landed cleanly,
    or the reconnect path pre-filled it), use that. Otherwise fall back
    to the account_id in the URL and upsert the User ourselves — the
    webhook is best-effort, not load-bearing.
    """
    # Land the user back where they started. Unipile redirects to our
    # success_redirect_url (already on the right host via _redirect_base), so
    # this same-origin "/" stays on event.surpluslayer.com when the flow began
    # there. Belt-and-suspenders: also honor the in-person host on this hop.
    from ..hosts import request_browser_host, is_inperson_host, is_first_party
    _h = request_browser_host(request)
    base_redirect = (f"https://{_h}/"
                     if _h and is_first_party(_h) and is_inperson_host(_h)
                     else "/")
    error_redirect = "/signin?error=linkedin_callback_failed"

    auth_state = db.query(AuthState).filter(AuthState.state_token == state).first()
    if not auth_state:
        return RedirectResponse(error_redirect, status_code=303)

    # Poll briefly for the webhook to land — short-circuit early if it
    # already wrote the LinkedIn profile fields. Capped at ~1.5s so we
    # degrade fast to the URL-based fallback when the webhook isn't going
    # to help us.
    #
    # Keying off `status` (not `user_id`) is important for Stripe-first
    # signup : /linkedin/start pre-tags auth_state.user_id to the prepay
    # user, so user_id-based polling would short-circuit BEFORE the webhook
    # writes name/headline/etc., leaving the user displayed as "Surplus
    # user." webhook_done means fields landed; pending means keep waiting.
    _DONE_STATES = {"webhook_done", "callback_upserted"}
    for _ in range(6):
        if auth_state.status in _DONE_STATES:
            break
        if auth_state.status == "failed":
            return RedirectResponse(error_redirect, status_code=303)
        await asyncio.sleep(0.25)
        db.refresh(auth_state)

    if auth_state.status not in _DONE_STATES:
        # Webhook didn't write fields. Use the account_id from the URL to
        # upsert the user ourselves. Pull profile fields from Unipile if we
        # have API creds; otherwise create a minimal record the operator
        # can fill in later.
        acct = (account_id or "").strip()
        if not acct:
            return RedirectResponse("/signin?error=linkedin_pending", status_code=303)
        dsn, api_key = _unipile_dsn(), _unipile_api_key()
        profile = await _fetch_unipile_profile(acct, dsn, api_key) if (dsn and api_key) else {}
        fields = _extract_profile_fields(profile)
        user = db.query(User).filter(User.unipile_account_id == acct).first()
        now = _utcnow()
        orphan_unipile_account_id: Optional[str] = None  # set when dedup fires
        if user is None and auth_state.user_id is not None:
            # Stripe-first signup : /linkedin/start pre-tagged the AuthState
            # with the paid prepay user's id. Adopt that row so the LinkedIn
            # fields merge into the same User (preserving paid_at) instead
            # of creating a duplicate. Matches the webhook path.
            user = db.query(User).filter(User.id == auth_state.user_id).first()
            if user is not None:
                print(f"  [auth.callback] adopting pre-tagged user.id={user.id} "
                      f"for new unipile_account_id={acct}")
                if user.unipile_account_id and user.unipile_account_id != acct:
                    orphan_unipile_account_id = user.unipile_account_id
                user.unipile_account_id = acct
        if user is None:
            # Dedup before insert : if the operator's site-data was cleared
            # (or they signed up via triage / email-only first), the new
            # Unipile account will have a fresh acct id but the SAME person
            # is behind it. Match on stable LinkedIn identifiers first
            # (provider_id then public_id), then fall back to email. Any
            # match means we CLAIM that existing User row : its Stripe
            # paid_at / customer_id / event ownership all stay intact, and
            # we just point its unipile_account_id at the new acct.
            for key, val in (
                ("linkedin_provider_id", fields.get("linkedin_provider_id")),
                ("linkedin_public_id", fields.get("linkedin_public_id")),
                ("email", fields.get("email")),
            ):
                if not val:
                    continue
                user = db.query(User).filter(
                    getattr(User, key) == val
                ).first()
                if user is not None:
                    print(f"  [auth.dedup] claimed existing user.id={user.id} "
                          f"via {key} : new unipile_account_id={acct}")
                    # Capture the old Unipile account_id so we can delete
                    # the orphan from Unipile *after* commit. Skip if it
                    # equals the new acct (no actual swap happening) or is
                    # already null (triage-signup path : no LinkedIn yet).
                    if user.unipile_account_id and user.unipile_account_id != acct:
                        orphan_unipile_account_id = user.unipile_account_id
                    user.unipile_account_id = acct
                    break
        if user:
            for k, v in fields.items():
                if v:
                    setattr(user, k, v)
            user.last_login_at = now
            user.linkedin_status = "active"
        else:
            user = User(
                unipile_account_id=acct,
                name=fields.get("name") or "LinkedIn user",
                email=fields.get("email"),
                headline=fields.get("headline"),
                avatar_url=fields.get("avatar_url"),
                linkedin_public_id=fields.get("linkedin_public_id"),
                linkedin_provider_id=fields.get("linkedin_provider_id"),
                last_login_at=now,
                linkedin_status="active",
            )
            db.add(user)
            db.flush()
        # First LinkedIn connection -> arm the onboarding tour (once per user).
        _arm_onboarding_if_first_connect(user)
        auth_state.user_id = user.id
        auth_state.status = "callback_upserted"
        db.commit()

        # This branch means the WEBHOOK no-op'd (no `name` echo), so the seed
        # never ran: fire it from here instead. Idempotent, so the rare case
        # where both paths dispatch is safe (skip-by-message-id).
        _autoimport_conversations(user.id)

        # Fire-and-forget : delete the orphan Unipile account that the
        # dedup migrated AWAY from. Done after commit so a Unipile delete
        # failure can't rollback the user-attachment. Best-effort.
        if orphan_unipile_account_id and dsn and api_key:
            await _delete_unipile_account(orphan_unipile_account_id,
                                          dsn, api_key)

    user = db.query(User).filter(User.id == auth_state.user_id).first()
    if not user:
        return RedirectResponse(error_redirect, status_code=303)

    sess = create_session(db, user)
    auth_state.status = "callback_done"
    auth_state.completed_at = _utcnow()
    db.commit()

    # Native app flow: hand the session token back via the surplus://auth deep
    # link (the app's system-browser login can't drop a usable cookie into the
    # WebView). The app then calls /api/auth/mobile-adopt?token=... to set the
    # cookie in its WebView. Skip the web Stripe redirect; the app reads billing
    # state from /me. The session is real either way, like the in-person host.
    if _is_mobile_state(auth_state.state_token):
        from urllib.parse import urlencode
        qs = urlencode({"token": sess.session_token})
        return RedirectResponse(f"{MOBILE_REDIRECT_URI}?{qs}", status_code=303)

    # Pay-at-connect paywall, tied to the LinkedIn identity. By here `user`
    # is the DEDUPED row for this LinkedIn account (matched on provider_id /
    # public_id / email above), so paid_at is portable across browsers and
    # devices : a LinkedIn that paid once is recognized everywhere and skips
    # Stripe. Only an unpaid LinkedIn is routed to Checkout, and only off the
    # in-person host (event.surpluslayer.com stays free). We set the session
    # cookie FIRST so the user returns from Stripe already signed in, and the
    # checkout is tagged with user.id so the webhook stamps THIS row.
    inperson = bool(_h and is_first_party(_h) and is_inperson_host(_h))
    if (not inperson) and user.paid_at is None:
        try:
            from .billing import build_checkout_url
            pay_url = build_checkout_url(request, db, user)
        except Exception as exc:  # noqa: BLE001 : Stripe/SDK/config errors
            print(f"  [auth.callback] pay-gate could not build checkout url "
                  f"for user.id={user.id} : {type(exc).__name__}: {exc}")
            pay_url = None
        if pay_url:
            response = RedirectResponse(pay_url, status_code=303)
            set_session_cookie(response, sess.session_token, host=_h)
            if user.unipile_account_id:
                set_last_account_cookie(response, user.unipile_account_id, host=_h)
            return response

    response = RedirectResponse(base_redirect, status_code=303)
    # Set the cookie Domain from the request host so the session is shared
    # across *.surpluslayer.com : on event.surpluslayer.com a host-only cookie
    # would be dropped on the next request and bounce the user back to login.
    set_session_cookie(response, sess.session_token, host=_h)
    # Persist the account_id so the NEXT sign-in from this browser goes
    # through type=reconnect : no new Unipile seat, usually no LinkedIn 2FA.
    if user.unipile_account_id:
        set_last_account_cookie(response, user.unipile_account_id, host=_h)
    return response


# ─── 3b-bis. Email connect (Gmail / Outlook via Unipile hosted-auth) ──
#
# A SECOND Unipile seat on the same workspace, pointing at the signed-in
# user's real mailbox. Unlike the LinkedIn flow above this is an
# INTEGRATION, not a sign-in method: the user already has a session, so
# there is no dedup/upsert gymnastics — the webhook just attaches the new
# account_id to their existing row. Flow:
#
#   1. POST /email/start (auth required) : mint AuthState(state_token,
#      user_id pre-tagged), POST /hosted/accounts/link with
#      providers=[GOOGLE, MICROSOFT], return {url}.
#   2. User authenticates on Unipile's hosted page (Google/Microsoft OAuth;
#      Unipile vaults the tokens — we only ever hold the account_id).
#   3. Unipile POSTs /email/webhook {status, account_id, name=state_token}:
#      stamp unipile_email_account_id + email_status='active' on the user.
#   4. Browser lands on GET /email/callback?state=... : brief-poll the
#      AuthState so the Integrations tile shows Connected immediately,
#      then redirect into the app.

# Map a frontend provider hint to Unipile's hosted-auth provider tokens.
# Unipile's token for Microsoft mail is "OUTLOOK" (the docs prose says
# "Microsoft" but the API schema rejects "MICROSOFT" with invalid_parameters,
# verified against the live API). An empty/unknown hint offers BOTH, so the
# generic "connect email" button keeps showing Unipile's provider picker.
def _email_providers_for(hint: str) -> list[str]:
    h = (hint or "").strip().lower()
    if h in ("outlook", "microsoft", "m365", "office365"):
        return ["OUTLOOK"]
    if h in ("google", "gmail", "workspace"):
        return ["GOOGLE"]
    return ["GOOGLE", "OUTLOOK"]


def _email_create_body(dsn: str, expires: str, state_token: str,
                       base: str, failure_url: str,
                       providers: Optional[list[str]] = None) -> dict:
    """Hosted-auth create body for the email channel. Mirrors _create_body
    but with the mail providers and the email webhook/callback URLs. A caller
    can pass a single-provider list (e.g. ["OUTLOOK"]) to mint a one-tap
    Outlook or Gmail link; default offers both."""
    return {
        "type": "create",
        "providers": providers or ["GOOGLE", "OUTLOOK"],
        "api_url": dsn,
        "expiresOn": expires,
        "success_redirect_url": f"{base}/api/auth/email/callback?state={state_token}",
        "failure_redirect_url": failure_url,
        "notify_url": f"{base}/api/auth/email/webhook",
        "name": state_token,
    }


def _extract_mailbox_address(account_data: dict) -> Optional[str]:
    """Best-effort pull of the connected mailbox address from a Unipile
    account payload. Email accounts surface it in different spots per
    provider; try the known ones and fall back to None (display-only
    field, nothing downstream depends on it)."""
    if not isinstance(account_data, dict):
        return None
    cp = account_data.get("connection_params") or {}
    mail = cp.get("mail") or {}
    for candidate in (
        mail.get("username"),
        mail.get("email"),
        cp.get("email"),
        account_data.get("email"),
        account_data.get("identifier"),
    ):
        if isinstance(candidate, str) and "@" in candidate:
            return candidate.strip().lower()
    return None


@router.post("/email/start")
async def email_start(
    request: Request,
    provider: str = "",
    db: DbSession = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    """Mint a hosted-auth link for connecting the signed-in user's mailbox.
    Auth required : the email seat always attaches to an existing account,
    so anonymous callers have nothing to attach it to.

    Optional ?provider=outlook (or google) mints a one-tap single-provider
    link; omitted, Unipile shows both Gmail and Outlook. Outlook goes fully
    through Unipile hosted auth, so no separate Microsoft/Azure app is needed."""
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    auth_state = AuthState(state_token=state_token, status="pending",
                           user_id=user.id)
    db.add(auth_state)
    db.commit()

    base = _redirect_base(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/?email_connect=failed"

    status_code, data = await _post_hosted_link(
        dsn, api_key,
        _email_create_body(dsn, expires, state_token, base, failure_url,
                           providers=_email_providers_for(provider)))
    url = (data or {}).get("url")
    if status_code >= 400 or not url:
        print(f"  [auth.email] hosted link failed status={status_code} "
              f"body={str(data)[:300]}")
        raise HTTPException(502, "couldn't start the email connect flow")
    return JSONResponse({"url": url})


@router.post("/email/webhook")
async def email_webhook(payload: dict,
                        db: DbSession = Depends(get_db)) -> JSONResponse:
    """Unipile posts here when a hosted-auth EMAIL account is created (or
    fails). Attaches the new account to the AuthState's pre-tagged user —
    no user creation, no dedup: email connect requires a signed-in caller."""
    status_raw = (payload.get("status") or "").upper()
    state_token = (payload.get("name") or "").strip()
    account_id = (payload.get("account_id") or "").strip()

    if not state_token:
        return JSONResponse({"ok": True, "ignored": "no state_token"})
    auth_state = (db.query(AuthState)
                  .filter(AuthState.state_token == state_token).first())
    if not auth_state:
        return JSONResponse({"ok": True, "ignored": "unknown state_token"})

    if status_raw not in {"CREATION_SUCCESS", "RECONNECTED"} or not account_id:
        auth_state.status = "failed"
        auth_state.error = f"unipile status={status_raw}"
        auth_state.completed_at = _utcnow()
        db.commit()
        return JSONResponse({"ok": True, "recorded": "failure"})

    user = (db.query(User).filter(User.id == auth_state.user_id).first()
            if auth_state.user_id is not None else None)
    if user is None:
        auth_state.status = "failed"
        auth_state.error = "no user on auth_state"
        auth_state.completed_at = _utcnow()
        db.commit()
        return JSONResponse({"ok": True, "ignored": "no user"})

    now = _utcnow()

    # Best-effort mailbox address for the Integrations tile ("Connected as
    # daniel@gmail.com"). Never fails the connect.
    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    addr = None
    if dsn and api_key:
        account_data = await _fetch_unipile_profile(account_id, dsn, api_key)
        addr = _extract_mailbox_address(account_data)

    # Find-or-create an EmailAccount for this mailbox and attach it to the
    # user. This creates an ADDITIONAL row for a second connect rather than
    # overwriting, handles the cross-user re-connect release/dedup at the
    # EmailAccount level, and keeps the legacy User.* fields mirrored to the
    # user's primary mailbox (so every existing read site keeps working).
    upsert_email_account(db, user=user, unipile_account_id=account_id,
                         address=addr, status="active")

    auth_state.status = "webhook_done"
    auth_state.completed_at = now
    db.commit()
    print(f"  [auth.email] attached email account {account_id} to "
          f"user.id={user.id} ({user.email_account_address or 'address n/a'})")

    # The magic moment: connect → the book fills itself. Kick the first
    # mailbox sync off the request lifecycle (own DB session, this webhook
    # must ack fast or Unipile retries). DURABLE (prefer_modal) so a deploy
    # mid-sync doesn't drop it. Best-effort by design; the manual
    # /api/relationships/email/sync route covers any failure.
    user_id = user.id
    if dsn and api_key:
        from ..jobs import run_detached
        run_detached(_email_first_sync, user_id, prefer_modal=True)

    return JSONResponse({"ok": True, "user_id": user.id})


@router.get("/email/callback")
async def email_callback(
    state: str = Query(...),
    db: DbSession = Depends(get_db),
):
    """Browser lands here after the hosted page. The WEBHOOK is the writer;
    this just gives it a few seconds to arrive so the Integrations tile
    reads Connected on the very next paint, then bounces into the app."""
    import asyncio as _asyncio
    for _ in range(10):  # up to ~5s
        auth_state = (db.query(AuthState)
                      .filter(AuthState.state_token == state).first())
        if auth_state and auth_state.status in ("webhook_done", "failed"):
            break
        await _asyncio.sleep(0.5)
        db.expire_all()  # re-read committed webhook writes, not session cache
    ok = bool(auth_state and auth_state.status == "webhook_done")
    return RedirectResponse(
        f"/?email_connect={'ok' if ok else 'pending'}", status_code=303)


# ─── 3b'. Connect WhatsApp (Unipile WHATSAPP account) ─────────────
#
# A THIRD Unipile seat on the same workspace, pointing at the signed-in
# user's WhatsApp account. WhatsApp is a CLOUD channel on Unipile (a hosted
# WhatsApp account -- like the LinkedIn + email seats above, NOT a device
# companion). Same INTEGRATION shape as the email connect: the user already
# has a session, so the webhook just attaches the new account_id to their
# existing row -- no dedup/upsert gymnastics. Flow:
#
#   1. POST /whatsapp/start (auth required) : mint AuthState(state_token,
#      user_id pre-tagged), POST /hosted/accounts/link with
#      providers=[WHATSAPP], return {url}.
#   2. User authenticates on Unipile's hosted page (WhatsApp QR pairing).
#   3. Unipile POSTs /whatsapp/webhook {status, account_id, name=state_token}:
#      stamp unipile_whatsapp_account_id + whatsapp_status='active' on the
#      user (with the cross-user release/dedup + orphan handling), then kick
#      a first sync.
#   4. Browser lands on GET /whatsapp/callback?state=... : brief-poll the
#      AuthState so the Connections tile shows Connected immediately.
#
# NOTE on the provider name: Unipile's hosted-auth provider token for the
# WhatsApp messaging channel is "WHATSAPP" (same family as "LINKEDIN" used by
# the LinkedIn seat). Verified against the Unipile hosted-auth provider list.

def _whatsapp_create_body(dsn: str, expires: str, state_token: str,
                          base: str, failure_url: str) -> dict:
    """Hosted-auth create body for the WhatsApp channel. Mirrors
    _email_create_body but with the WHATSAPP provider and the whatsapp
    webhook/callback URLs."""
    return {
        "type": "create",
        "providers": ["WHATSAPP"],
        "api_url": dsn,
        "expiresOn": expires,
        "success_redirect_url": f"{base}/api/auth/whatsapp/callback?state={state_token}",
        "failure_redirect_url": failure_url,
        "notify_url": f"{base}/api/auth/whatsapp/webhook",
        "name": state_token,
    }


@router.post("/whatsapp/start")
async def whatsapp_start(
    request: Request,
    db: DbSession = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    """Mint a hosted-auth link for connecting the signed-in user's WhatsApp.
    Auth required : the WhatsApp seat always attaches to an existing account,
    so anonymous callers have nothing to attach it to."""
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    auth_state = AuthState(state_token=state_token, status="pending",
                           user_id=user.id)
    db.add(auth_state)
    db.commit()

    base = _redirect_base(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/?whatsapp_connect=failed"

    status_code, data = await _post_hosted_link(
        dsn, api_key,
        _whatsapp_create_body(dsn, expires, state_token, base, failure_url))
    url = (data or {}).get("url")
    if status_code >= 400 or not url:
        print(f"  [auth.whatsapp] hosted link failed status={status_code} "
              f"body={str(data)[:300]}")
        raise HTTPException(502, "couldn't start the WhatsApp connect flow")
    return JSONResponse({"url": url})


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(payload: dict,
                           db: DbSession = Depends(get_db)) -> JSONResponse:
    """Unipile posts here when a hosted-auth WHATSAPP account is created (or
    fails). Attaches the new account to the AuthState's pre-tagged user. Like
    the email webhook this requires a signed-in caller (no user creation), but
    it also releases the account_id from any OTHER user that held it (a
    re-connect of the same WhatsApp from a different seat) so the UNIQUE index
    on users.unipile_whatsapp_account_id never rejects the write, and orphans
    the previously-attached account for a post-commit Unipile delete."""
    status_raw = (payload.get("status") or "").upper()
    state_token = (payload.get("name") or "").strip()
    account_id = (payload.get("account_id") or "").strip()

    if not state_token:
        return JSONResponse({"ok": True, "ignored": "no state_token"})
    auth_state = (db.query(AuthState)
                  .filter(AuthState.state_token == state_token).first())
    if not auth_state:
        return JSONResponse({"ok": True, "ignored": "unknown state_token"})

    if status_raw not in {"CREATION_SUCCESS", "RECONNECTED"} or not account_id:
        auth_state.status = "failed"
        auth_state.error = f"unipile status={status_raw}"
        auth_state.completed_at = _utcnow()
        db.commit()
        return JSONResponse({"ok": True, "recorded": "failure"})

    user = (db.query(User).filter(User.id == auth_state.user_id).first()
            if auth_state.user_id is not None else None)
    if user is None:
        auth_state.status = "failed"
        auth_state.error = "no user on auth_state"
        auth_state.completed_at = _utcnow()
        db.commit()
        return JSONResponse({"ok": True, "ignored": "no user"})

    now = _utcnow()

    # Cross-user release : if this account_id is already attached to a DIFFERENT
    # user, detach it there first (and orphan THAT user's prior account if it
    # differs) so the UNIQUE index doesn't reject our UPDATE. Mirrors the
    # release/dedup the email + LinkedIn flows do.
    orphan_unipile_account_id: Optional[str] = None
    other = (db.query(User)
             .filter(User.unipile_whatsapp_account_id == account_id,
                     User.id != user.id).first())
    if other is not None:
        other.unipile_whatsapp_account_id = None
        other.whatsapp_status = "disconnected"
        other.whatsapp_connected_at = None
    if (user.unipile_whatsapp_account_id
            and user.unipile_whatsapp_account_id != account_id):
        orphan_unipile_account_id = user.unipile_whatsapp_account_id

    user.unipile_whatsapp_account_id = account_id
    user.whatsapp_status = "active"
    user.whatsapp_connected_at = now

    auth_state.status = "webhook_done"
    auth_state.completed_at = now
    db.commit()
    print(f"  [auth.whatsapp] attached whatsapp account {account_id} to "
          f"user.id={user.id}")

    dsn, api_key = _unipile_dsn(), _unipile_api_key()

    # Best-effort: drop the orphaned (replaced) account from Unipile after
    # commit, mirroring the LinkedIn/email orphan handling.
    if orphan_unipile_account_id and dsn and api_key:
        await _delete_unipile_account(orphan_unipile_account_id, dsn, api_key)

    # The magic moment: connect -> the book fills itself. The first WhatsApp
    # sync is MINUTES of Unipile I/O (page chats, fetch each conversation), so
    # it must NOT run inside this webhook's request lifecycle -- an in-request
    # thread gets killed when the worker recycles and the user is left with 0
    # conversations. Dispatch it DURABLY (Modal when USE_MODAL is on, else a
    # daemon thread that owns its own DB session) so it survives the ack and can
    # take its time. Idempotent (skip-by-message-id) so a retry is safe.
    user_id = user.id
    if dsn and api_key:
        from ..jobs import dispatch_whatsapp_first_sync
        runner = dispatch_whatsapp_first_sync(user_id)
        print(f"  [auth.whatsapp] dispatched first sync user.id={user_id} "
              f"runner={runner}")

    return JSONResponse({"ok": True, "user_id": user.id})


@router.get("/whatsapp/callback")
async def whatsapp_callback(
    state: str = Query(...),
    db: DbSession = Depends(get_db),
):
    """Browser lands here after the hosted page. The WEBHOOK is the writer;
    this just gives it a few seconds to arrive so the Connections tile reads
    Connected on the very next paint, then bounces into the app."""
    import asyncio as _asyncio
    for _ in range(10):  # up to ~5s
        auth_state = (db.query(AuthState)
                      .filter(AuthState.state_token == state).first())
        if auth_state and auth_state.status in ("webhook_done", "failed"):
            break
        await _asyncio.sleep(0.5)
        db.expire_all()  # re-read committed webhook writes, not session cache
    ok = bool(auth_state and auth_state.status == "webhook_done")
    return RedirectResponse(
        f"/?whatsapp_connect={'ok' if ok else 'pending'}", status_code=303)


# ─── 3c. Triage quick-start : zero-friction anonymous session ─────
#
# For demos / first-time users who just want to upload a CSV and see
# results. No email, no LinkedIn, no form. Click 'Triage mode' button,
# this endpoint mints a User row + session, and the operator lands
# straight in the triage flow. They can attach an email later if they
# want to recover the data across browsers.

@router.post("/triage/quick-start",
             dependencies=[Depends(_rl_triage_signup)])
def triage_quick_start(db: DbSession = Depends(get_db),
                       request: Request = None) -> JSONResponse:
    """Create an anonymous User row + session cookie. Caller reloads and
    lands in TriageApp (App.jsx routes there for users with no
    unipile_account_id)."""
    # Random suffix in email so the unique constraint doesn't collide if
    # the same browser hits this twice. Email lives in our DB only,
    # nothing's ever sent to it.
    tag = secrets.token_hex(6)
    user = User(
        name="Triage user",
        email=f"triage-{tag}@anonymous.surplus",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sess = create_session(db, user)
    resp = JSONResponse({"ok": True, "user_id": user.id, "mode": "triage_only"})
    set_session_cookie(resp, sess.session_token,
                       host=request.headers.get("host") if request else None)
    return resp


# ─── 3a'. In-person guest : zero-friction anonymous session ───────────
#
# For the phone-first in-person surface (event.surpluslayer.com). Lets a tester
# use the capture flow without LinkedIn : creates a throwaway, LinkedIn-LESS User
# + session so scan / resolve / draft / save all work. Real LinkedIn SENDS stay
# blocked (no unipile_account_id -> the existing send gate / "Connect LinkedIn
# to send" banner), so a guest can never send from anyone's account.
#
# Gated to the in-person host (X-Forwarded-Host aware) so this guest door only
# exists on event.surpluslayer.com, never on the apex product.

@router.post("/inperson/guest",
             dependencies=[Depends(_rl_triage_signup)])
def inperson_guest(request: Request, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Mint an anonymous, LinkedIn-less guest session for the in-person host.
    403 on any non-in-person host so the apex product keeps its sign-in gate."""
    from ..hosts import request_browser_host, is_inperson_host
    host = request_browser_host(request)
    if not is_inperson_host(host):
        raise HTTPException(status_code=403,
                            detail="guest access is only available on the in-person host")
    tag = secrets.token_hex(6)
    user = User(
        name="Guest",
        email=f"guest-{tag}@anonymous.surplus",
        # NOTE: no unipile_account_id -> not LinkedIn-connected -> cannot send.
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sess = create_session(db, user)
    resp = JSONResponse({"ok": True, "user_id": user.id, "mode": "inperson_guest"})
    set_session_cookie(resp, sess.session_token, host=host)
    return resp


# ─── 3b. Triage-only signup (no LinkedIn / no Unipile) ─────────────
#
# Customers who only want to use Applicant Triage (review Luma applicants)
# don't need LinkedIn outreach. Forcing them through Unipile auth would be
# pointless friction and a billed seat we don't need to spend. They get a
# User row with unipile_account_id=NULL : full app access except outbound
# LinkedIn features, which gate on having a Unipile connection.

class TriageSignupBody(BaseModel):
    name: str
    email: str


@router.post("/triage/signup",
             dependencies=[Depends(_rl_triage_signup_email)])
def triage_signup(
    body: TriageSignupBody,
    db: DbSession = Depends(get_db),
    request: Request = None,
) -> JSONResponse:
    """Create a User row + session for someone who only wants triage.

    No email verification : trust scales later. This endpoint is intended
    for self-serve signup from the public sign-in screen, not for the
    operator's main flow (which still goes through LinkedIn).

    Existing email → returns the existing User + a fresh session, so a
    second signup attempt doesn't crash on the unique-ish email constraint.
    """
    name = (body.name or "").strip()
    email = (body.email or "").strip().lower()
    if not name or not email or "@" not in email:
        raise HTTPException(400, "name and a valid email are required")

    # Reuse existing User row if email matches : prevents accidental dupes.
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(name=name, email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        # TOCTOU backstop. users.email has NO unique constraint, so two
        # concurrent signups (double-click, retried request) can BOTH pass
        # the read above and BOTH insert — a silent duplicate identity that
        # later splits the account's data. Converge deterministically: every
        # racer re-reads, adopts the OLDEST row for this email, and the
        # losers delete their own insert (no session points at it yet).
        oldest = (db.query(User).filter(User.email == email)
                  .order_by(User.id.asc()).first())
        if oldest is not None and oldest.id != user.id:
            db.delete(user)
            db.commit()
            user = oldest

    sess = create_session(db, user)
    # Cookie has to be set on the SAME response we return : FastAPI gotcha
    # where setting headers/cookies on a dependency-injected Response is
    # ignored when the handler returns a different Response instance.
    resp = JSONResponse({
        "ok": True,
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "mode": "triage_only",
    })
    set_session_cookie(resp, sess.session_token,
                       host=request.headers.get("host") if request else None)
    return resp


# ─── 4. /me: who is signed in? ────────────────────────────────────

def _autonomy_mode(user) -> str:
    """The user's normalized autonomy mode for the /me payload. Thin local
    wrapper so /me does not import the send pipeline at module load."""
    from ..agents.relationship.pipeline.send.sender import owner_autonomy_mode
    return owner_autonomy_mode(user)


@router.get("/me")
def me(user: User = Depends(current_user),
       db: DbSession = Depends(get_db)) -> JSONResponse:
    # All connected mailboxes (primary first). The legacy single-mailbox keys
    # below (email_status / email_account_address / unipile_email_account_id)
    # are kept for compatibility and always reflect the PRIMARY account.
    # db may be the unresolved Depends() sentinel for direct unit-test callers
    # (me(user)) -- fall back to the user's bound session so the mailbox list
    # still resolves. At runtime FastAPI always injects a real Session.
    from sqlalchemy.orm import Session as _Session, object_session
    if not isinstance(db, _Session):
        db = object_session(user)
    email_accounts = [
        {"address": a.address, "status": a.status, "provider": a.provider,
         "is_primary": a.is_primary, "unipile_account_id": a.unipile_account_id}
        for a in list_email_accounts(db, user)
    ] if db is not None else []
    # Google Calendar/Contacts (OAuth ConnectedAccount). Returned here so the
    # Connections screen renders that row INSTANTLY from /me, with no separate
    # per-open listIntegrations fetch (which caused the load-on-click lag).
    google_connected = bool(
        db is not None and db.query(ConnectedAccount)
        .filter_by(user_id=user.id, provider="google", status="active").first()
    )
    return JSONResponse({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        # Whether an email+password credential is set. False for OAuth-only
        # accounts (Google/LinkedIn) — the account settings UI shows "Set a
        # password" vs "Change password" off this.
        "has_password": bool(getattr(user, "password_hash", None)),
        "headline": user.headline,
        "avatar_url": user.avatar_url,
        "linkedin_public_id": user.linkedin_public_id,
        "linkedin_status": user.linkedin_status,
        "unipile_account_id": user.unipile_account_id,
        # Email channel (Gmail/Outlook via Unipile). The Integrations tile
        # branches on email_status; email_account_address is the display
        # line ("Connected as daniel@gmail.com").
        "email_status": getattr(user, "email_status", "disconnected") or "disconnected",
        "email_account_address": getattr(user, "email_account_address", None),
        "unipile_email_account_id": getattr(user, "unipile_email_account_id", None),
        # WhatsApp channel (a CLOUD Unipile seat). The Connections tile branches
        # on whatsapp_status; mirrors the email_status shape.
        "whatsapp_status": getattr(user, "whatsapp_status", "disconnected") or "disconnected",
        "unipile_whatsapp_account_id": getattr(user, "unipile_whatsapp_account_id", None),
        # All connected mailboxes (Gmail/Outlook). Empty for users with no
        # email connected. The Connections screen renders one row per entry.
        "email_accounts": email_accounts,
        # Google Calendar/Contacts connection -- lets the Connections row show
        # instant on/off from /me (no separate fetch).
        "google_connected": google_connected,
        # True for sessions that entered via the hidden demo link. The SPA
        # uses this to hide demo-only surfaces (e.g. the ROI ledger stage).
        "is_demo": is_demo_user(user),
        # Billing state. paid_at is null for free-tier users; once stamped
        # by the Stripe webhook (or the dev-toggle endpoint) the SPA can
        # branch on it to hide the "Upgrade" CTA. stripe_customer_id is
        # populated by the webhook on successful checkout.
        "paid_at": user.paid_at.isoformat() if user.paid_at else None,
        "stripe_customer_id": user.stripe_customer_id,
        # Relationship-layer metered plan (independent of paid_at). The SPA
        # reads this to render the usage meter (X/5 drafts left) and decide
        # whether to show the pricing table. unlimited=True for demo /
        # allowlisted accounts.
        "billing": bp.usage_snapshot(user),
        # First-time-user onboarding tour. The in-person surface reads these
        # to decide whether to run the coachmark flow ('active') and which
        # step to resume from. saved_send_link is the user's reusable demo /
        # Calendly link, pre-filled on every future send.
        "onboarding_status": getattr(user, "onboarding_status", "") or "",
        "onboarding_step": getattr(user, "onboarding_step", 0) or 0,
        "saved_send_link": getattr(user, "saved_send_link", None),
        # Per-user autonomy control ('off' | 'ask' | 'auto', normalized). The
        # SPA reads this to render the Account setting and, in 'ask' mode, the
        # Today "Waiting for your OK" queue. Written via PUT /api/settings.
        "autonomy_mode": _autonomy_mode(user),
    })


# ─── Plugin session token + cross-context bootstrap ──────────────────
#
# The Chrome extension embeds the in-person Book as an iframe pointed at
# event.surpluslayer.com. Under Chrome storage-partitioning, that iframe's
# cookie jar is keyed to the EXTENSION origin, NOT shared with a standalone
# event.surpluslayer.com tab. So the extension's service-worker fetches (which
# ride the extension's partitioned cookie jar) and the embedded iframe can end
# up authenticated as a different account than (or no account at all, vs) the
# user's first-party web tab.
#
# Fix: let the extension hold ONE plugin session token (Bearer) and replay it
# into the iframe so BOTH contexts resolve to the same User:
#
#   1. POST /plugin/token   (cookie- OR bearer-authenticated)
#        Mints a client="plugin" Session for the signed-in user and returns its
#        token. The extension caches it and sends it as `Authorization: Bearer`
#        on every service-worker API call.
#   2. GET  /token-bootstrap?token=<plugin_token>&next=/
#        Same-origin GET the extension points the iframe at FIRST. It validates
#        the token against the Session table and, if live, sets the first-party
#        surplus_session cookie to that token IN THE IFRAME'S PARTITION, then
#        303s to `next`. After this the BookApp in the iframe is the same account
#        as the extension's Bearer calls -- no reliance on a shared cookie jar.
#
# Security notes:
#   - /plugin/token requires an already-authenticated caller (current_user); it
#     never mints a session for an anonymous request. It only re-issues a token
#     for the user you're already signed in as.
#   - /token-bootstrap only ever ADOPTS an existing, non-revoked, unexpired
#     session token -- it cannot create a session or escalate. The cookie it
#     sets is HttpOnly + Secure (prod) + SameSite=Lax, same as every other
#     session cookie, and `next` is constrained to a same-origin path so it
#     can't be turned into an open redirect or used to set a cookie on a
#     foreign host.
#   - Tokens are opaque 32-byte secrets; we never log their value.


@router.post("/plugin/token")
def plugin_token(
    db: DbSession = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    """Mint (or re-issue) a client="plugin" session token for the signed-in
    user. The extension caches this and uses it as a Bearer token for its
    service-worker API calls AND replays it into the embedded Book iframe via
    /token-bootstrap so both contexts share one account.

    Auth required: this only ever issues a token for the user you're already
    authenticated as (cookie or existing Bearer). It cannot be used to obtain a
    session for someone else."""
    sess = create_session(db, user, client="plugin")
    return JSONResponse({"token": sess.session_token, "user_id": user.id})


@router.get("/token-bootstrap")
def token_bootstrap(
    request: Request,
    token: str = Query(...),
    next: str = Query("/"),
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    """Adopt an existing session token into the FIRST-PARTY cookie for THIS
    browsing context (the extension points its Book iframe here first). Sets
    surplus_session to `token` only if it resolves to a live session, then
    redirects to `next` (same-origin path only). Never creates or escalates a
    session -- it only mirrors an already-valid token into the cookie jar of
    the partitioned iframe so the SPA authenticates as the same user.

    On a bad/expired/revoked token we DON'T set a cookie and just bounce to
    `next` so the SPA falls through to its normal signed-out sign-in screen."""
    # Constrain `next` to a same-origin path so this can't be turned into an
    # open redirect (and the cookie we set always belongs to our own host).
    safe_next = next if (isinstance(next, str) and next.startswith("/")
                         and not next.startswith("//")) else "/"

    # SECURITY: refuse to set a session cookie on a cross-site TOP-LEVEL click
    # (login-CSRF / session fixation — a link that pins the victim to an
    # attacker-supplied token). The legitimate caller is the extension's Book
    # IFRAME (Sec-Fetch-Dest: iframe), never a top-level document navigation.
    if _is_cross_site_toplevel_nav(request):
        return RedirectResponse(safe_next, status_code=303)

    user = _load_user_by_session(db, (token or "").strip() or None)
    host = request_browser_host(request)
    resp = RedirectResponse(safe_next, status_code=303)
    if user is not None:
        # Mirror the valid plugin token into the first-party session cookie for
        # this (iframe) partition. Same cookie attributes as every other login.
        set_session_cookie(resp, (token or "").strip(), host=host)
        _make_cookie_partition_friendly(resp)
    return resp


def _make_cookie_partition_friendly(resp: Response) -> None:
    """Make the surplus_session Set-Cookie usable inside a PARTITIONED
    third-party iframe (the extension's embedded Book).

    Modern Chrome (3rd-party-cookie phase-out) won't STORE an unpartitioned
    cookie set in a third-party frame. CHIPS fixes this: a cookie marked
    `Partitioned` (which requires `Secure` and `SameSite=None`) is stored in a
    jar keyed to the top-level (extension) origin -- exactly the context the
    iframe runs in. Starlette 0.38 has no `partitioned=` kwarg, so we rewrite
    the header we just set.

    Only applied over HTTPS (the cookie is Secure): `Partitioned` is invalid
    without Secure, and local-http dev keeps the plain Lax cookie (no
    partitioning needed there -- the dev iframe is same-site). This ONLY touches
    the bootstrap response; the normal first-party login cookie is unchanged.
    """
    cookies = resp.raw_headers  # list[tuple[bytes, bytes]]
    rewritten = []
    for name, value in cookies:
        if name.lower() == b"set-cookie" and value.lstrip().lower().startswith(
            SESSION_COOKIE.lower().encode() + b"="
        ) and b"secure" in value.lower():
            v = value
            # Flip SameSite=Lax -> None (required for a cross-site partitioned
            # cookie to be sent inside the iframe), then append Partitioned.
            v = v.replace(b"SameSite=lax", b"SameSite=None").replace(
                b"SameSite=Lax", b"SameSite=None")
            if b"partitioned" not in v.lower():
                v = v + b"; Partitioned"
            rewritten.append((name, v))
        else:
            rewritten.append((name, value))
    resp.raw_headers[:] = rewritten


# ─── Onboarding tour state ─────────────────────────────────────────

class OnboardingPatch(BaseModel):
    # Which coachmark the user is on (0-based). Optional so a caller can
    # update just the status (e.g. skip) without moving the step.
    step: Optional[int] = None
    # "active" | "done" | "skipped". "active" + step 0 re-runs the tour from
    # settings. Omitted -> status unchanged.
    status: Optional[str] = None


@router.put("/onboarding")
def update_onboarding(
    patch: OnboardingPatch,
    db: DbSession = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    """Persist onboarding progress so the tour survives a refresh / device
    switch. The in-person surface PUTs here on every advance, on skip, and on
    'replay tour' from settings."""
    if patch.step is not None:
        user.onboarding_step = max(0, int(patch.step))
    if patch.status is not None:
        status = (patch.status or "").strip().lower()
        if status in {"active", "done", "skipped"}:
            user.onboarding_status = status
            if status == "active" and patch.step is None:
                # Replay from the top unless the caller pinned a step.
                user.onboarding_step = 0
    db.commit()
    return JSONResponse({
        "onboarding_status": user.onboarding_status or "",
        "onboarding_step": user.onboarding_step or 0,
    })


# ─── Startup backfill : repopulate dedup keys on existing User rows ──
#
# Before the _extract_profile_fields camelCase fix, every existing User row
# had NULL linkedin_provider_id / linkedin_public_id, so the dedup loop in
# linkedin_callback / linkedin_webhook could never match an incoming sign-in
# to an existing user. That cascade produced duplicate Unipile accounts in
# the dashboard AND, worse, fresh User rows with paid_at=NULL — meaning a
# previously-paid user would be forced through Stripe Checkout again the
# moment they cleared cookies. Critical for prod billing.
#
# This runs once at startup. For each User row missing dedup keys, we hit
# the Unipile /accounts/<id> endpoint, re-run _extract_profile_fields with
# the now-correct keys, and write whatever we get back. Best-effort : a
# Unipile 404 just leaves the row alone (the user will heal on their
# next real sign-in).

async def backfill_user_dedup_keys() -> None:
    """One-shot async backfill. Idempotent and safe to run on every boot."""
    import httpx
    from ..db import SessionLocal
    from ..models import User

    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    if not (dsn and api_key):
        print("  [auth.backfill] skipped : no Unipile DSN / API key")
        return

    db = SessionLocal()
    try:
        candidates = db.query(User).filter(
            User.unipile_account_id.isnot(None),
            (User.linkedin_provider_id.is_(None))
            | (User.linkedin_public_id.is_(None)),
        ).all()
        if not candidates:
            print("  [auth.backfill] no users need dedup-key backfill")
            return
        print(f"  [auth.backfill] backfilling dedup keys for "
              f"{len(candidates)} user(s)")
        # Throttle to ~5 req/s so a fresh deploy can't burn the workspace's
        # Unipile quota in a tight loop. On HTTP 429 we back off and bail
        # out of the rest of the batch — they'll get picked up on the next
        # boot. Sequential (not asyncio.gather) for the same reason.
        async with httpx.AsyncClient(timeout=15) as client:
            for u in candidates:
                try:
                    r = await client.get(
                        f"{dsn}/api/v1/accounts/{u.unipile_account_id}",
                        headers={"X-API-KEY": api_key, "Accept": "application/json"},
                    )
                    if r.status_code == 429:
                        print(f"  [auth.backfill] HIT Unipile 429 at "
                              f"user.id={u.id} — bailing out; remaining "
                              f"users will be tried on next boot")
                        break
                    if r.status_code >= 400:
                        print(f"  [auth.backfill] user.id={u.id} "
                              f"unipile_account_id={u.unipile_account_id} "
                              f"→ HTTP {r.status_code} (orphan, skipped)")
                        continue
                    fields = _extract_profile_fields(r.json() or {})
                except Exception as exc:  # noqa: BLE001
                    print(f"  [auth.backfill] user.id={u.id} fetch error: {exc}")
                    continue
                wrote = []
                for k, v in fields.items():
                    if v and getattr(u, k, None) != v:
                        setattr(u, k, v)
                        wrote.append(k)
                if wrote:
                    print(f"  [auth.backfill] user.id={u.id} updated: {wrote}")
                await asyncio.sleep(0.2)  # ~5 req/s ceiling
        db.commit()
    finally:
        db.close()


# ─── 5. Logout ────────────────────────────────────────────────────

@router.post("/logout")
def logout(
    response: Response,
    request: Request,
    db: DbSession = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    # Revoke whichever transport the caller used. The web app sends the cookie;
    # the extension (partitioned cookie jar) signs out via its plugin Bearer
    # token, so honor that too -- otherwise an extension sign-out would leave
    # the plugin session live and the iframe could re-adopt it.
    from ..auth import _bearer_token
    for token in (request.cookies.get(SESSION_COOKIE), _bearer_token(authorization)):
        if token:
            revoke_session(db, token)
    from ..hosts import request_browser_host
    # Clear the cookie on the RETURNED response. Mutating the injected `response`
    # is a no-op here: FastAPI sends the JSONResponse we return and discards the
    # injected response's headers, so the Set-Cookie delete would be dropped and
    # the (now-revoked) cookie would linger in the browser.
    out = JSONResponse({"ok": True})
    clear_session_cookie(out, host=request_browser_host(request))
    return out
