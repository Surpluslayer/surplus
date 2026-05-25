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
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from ..auth import (
    DEMO_USER_EMAIL,
    LAST_ACCOUNT_COOKIE,
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    current_user,
    require_paid_to_connect_linkedin,
    revoke_session,
    set_last_account_cookie,
    set_session_cookie,
)
from ..db import get_db
from ..models import AuthState, Session, User


router = APIRouter(prefix="/api/auth", tags=["auth"])


# ─── Unipile config + HTTP helpers ─────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _unipile_dsn() -> Optional[str]:
    raw = (os.environ.get("UNIPILE_DSN", "") or "").strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


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
      3. The request's own origin, forcing https:// for production hosts
         (local dev / preview builds)

    Always force https:// for surpluslayer.com / railway.app hosts :
    Railway terminates SSL upstream so request.url.scheme is "http" but
    the user-facing URL is "https"."""
    env = (os.environ.get("SURPLUS_BASE_URL", "") or "").strip().rstrip("/")
    if env:
        return env
    host = request.url.netloc
    if any(host == h or host.startswith(h + ":") for h in _PRODUCTION_ORIGIN_HOSTS):
        return _PRODUCTION_APEX
    # Trust X-Forwarded-Proto if present, otherwise infer https for production hosts
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    scheme = forwarded or request.url.scheme
    if "surpluslayer.com" in host or "railway.app" in host:
        scheme = "https"
    return f"{scheme}://{host}"


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
    """Look up the User whose Unipile account_id matches the cookie,
    if any. Returns None on missing/stale cookie : caller falls back
    to create."""
    last_account = (request.cookies.get(LAST_ACCOUNT_COOKIE) or "").strip()
    if not last_account:
        return None
    return db.query(User).filter(User.unipile_account_id == last_account).first()


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


@router.post("/linkedin/start")
async def linkedin_start(
    request: Request,
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    auth_state = AuthState(state_token=state_token, status="pending")
    db.add(auth_state)
    db.flush()  # populate auth_state.id without committing yet

    base = _surplus_base_url(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/signin?error=linkedin_auth_failed"

    # ── Paywall : signed-in users must have paid before connecting LinkedIn.
    # Anonymous callers (first-time signup via LinkedIn) sail through
    # unchanged. _resolve_returning_user reads the cookies; if a
    # signed-in User row is returned and they haven't paid, reject with
    # 402 payment_required (frontend opens Stripe Checkout).
    returning = _resolve_returning_user(request, db)
    # Also check the active session cookie : _resolve_returning_user reads
    # the last_account cookie which can be stale; the session cookie is
    # the source of truth for "is someone signed in right now."
    session_token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    active_user = (db.query(User).join(Session)
                   .filter(Session.session_token == session_token,
                           Session.revoked_at.is_(None))
                   .first()
                   if session_token else None)
    require_paid_to_connect_linkedin(active_user)

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
            auth_state.user_id = None  # un-prefill : webhook will resolve
            db.commit()
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


# ─── 1b. Redirect-style start (for landing-page links) ────────────
# The POST /linkedin/start returns JSON for in-app fetch flows. This
# variant accepts a top-level GET navigation (e.g. clicked from
# join.surpluslayer.com) and 303-redirects the user straight to the
# Unipile hosted-auth URL so the cookie set on /linkedin/callback
# arrives in the same browser-driven navigation chain.

@router.get("/linkedin/start-redirect")
async def linkedin_start_redirect(
    request: Request,
    db: DbSession = Depends(get_db),
):
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    auth_state = AuthState(state_token=state_token, status="pending")
    db.add(auth_state)
    db.flush()

    base = _surplus_base_url(request)
    expires = _unipile_iso_timestamp(_utcnow() + timedelta(hours=1))
    failure_url = f"{base}/?error=linkedin_auth_failed"

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
            auth_state.user_id = None
            db.commit()
            body = _create_body(dsn, expires, state_token, base, failure_url)
            status, data = await _post_hosted_link(dsn, api_key, body)
        if status >= 400 or not data.get("url"):
            return RedirectResponse(
                f"{base}/?error=linkedin_unipile_rejected", status_code=303,
            )
        return RedirectResponse(data["url"], status_code=303)
    except httpx.HTTPError:
        return RedirectResponse(f"{base}/?error=linkedin_unreachable", status_code=303)


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

    auth_state.user_id = user.id
    auth_state.status = "webhook_done"
    auth_state.completed_at = now
    db.commit()
    return JSONResponse({"ok": True, "user_id": user.id})


# ─── 3. Callback: user lands here after Unipile auth ───────────────

@router.get("/linkedin/callback")
async def linkedin_callback(
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
    base_redirect = "/"
    error_redirect = "/signin?error=linkedin_callback_failed"

    auth_state = db.query(AuthState).filter(AuthState.state_token == state).first()
    if not auth_state:
        return RedirectResponse(error_redirect, status_code=303)

    # Poll briefly for the webhook to land — short-circuit early if it
    # already did. Capped at ~1.5s so we degrade fast to the URL-based
    # fallback when the webhook isn't going to help us.
    for _ in range(6):
        if auth_state.user_id is not None:
            break
        if auth_state.status == "failed":
            return RedirectResponse(error_redirect, status_code=303)
        await asyncio.sleep(0.25)
        db.refresh(auth_state)

    if auth_state.user_id is None:
        # Webhook didn't link. Use the account_id from the URL to upsert
        # the user ourselves. Pull profile fields from Unipile if we have
        # API creds; otherwise create a minimal record the operator can
        # fill in later.
        acct = (account_id or "").strip()
        if not acct:
            return RedirectResponse("/signin?error=linkedin_pending", status_code=303)
        dsn, api_key = _unipile_dsn(), _unipile_api_key()
        profile = await _fetch_unipile_profile(acct, dsn, api_key) if (dsn and api_key) else {}
        fields = _extract_profile_fields(profile)
        user = db.query(User).filter(User.unipile_account_id == acct).first()
        now = _utcnow()
        orphan_unipile_account_id: Optional[str] = None  # set when dedup fires
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
        auth_state.user_id = user.id
        auth_state.status = "callback_upserted"
        db.commit()

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

    response = RedirectResponse(base_redirect, status_code=303)
    set_session_cookie(response, sess.session_token)
    # Persist the account_id so the NEXT sign-in from this browser goes
    # through type=reconnect : no new Unipile seat, usually no LinkedIn 2FA.
    if user.unipile_account_id:
        set_last_account_cookie(response, user.unipile_account_id)
    return response


# ─── 3c. Triage quick-start : zero-friction anonymous session ─────
#
# For demos / first-time users who just want to upload a CSV and see
# results. No email, no LinkedIn, no form. Click 'Triage mode' button,
# this endpoint mints a User row + session, and the operator lands
# straight in the triage flow. They can attach an email later if they
# want to recover the data across browsers.

@router.post("/triage/quick-start")
def triage_quick_start(db: DbSession = Depends(get_db)) -> JSONResponse:
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
    set_session_cookie(resp, sess.session_token)
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


@router.post("/triage/signup")
def triage_signup(
    body: TriageSignupBody,
    db: DbSession = Depends(get_db),
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
    set_session_cookie(resp, sess.session_token)
    return resp


# ─── 4. /me: who is signed in? ────────────────────────────────────

@router.get("/me")
def me(user: User = Depends(current_user)) -> JSONResponse:
    return JSONResponse({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "headline": user.headline,
        "avatar_url": user.avatar_url,
        "linkedin_public_id": user.linkedin_public_id,
        "linkedin_status": user.linkedin_status,
        "unipile_account_id": user.unipile_account_id,
        # True for sessions that entered via the hidden demo link. The SPA
        # uses this to hide demo-only surfaces (e.g. the ROI ledger stage).
        "is_demo": user.email == DEMO_USER_EMAIL,
        # Billing state. paid_at is null for free-tier users; once stamped
        # by the Stripe webhook (or the dev-toggle endpoint) the SPA can
        # branch on it to hide the "Upgrade" CTA. stripe_customer_id is
        # populated by the webhook on successful checkout.
        "paid_at": user.paid_at.isoformat() if user.paid_at else None,
        "stripe_customer_id": user.stripe_customer_id,
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
        async with httpx.AsyncClient(timeout=15) as client:
            for u in candidates:
                try:
                    r = await client.get(
                        f"{dsn}/api/v1/accounts/{u.unipile_account_id}",
                        headers={"X-API-KEY": api_key, "Accept": "application/json"},
                    )
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
        db.commit()
    finally:
        db.close()


# ─── 5. Logout ────────────────────────────────────────────────────

@router.post("/logout")
def logout(
    response: Response,
    request: Request,
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(db, token)
    clear_session_cookie(response)
    return JSONResponse({"ok": True})
