"""
routes/auth.py — Sign in with LinkedIn (via Unipile hosted-auth).

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
from sqlalchemy.orm import Session as DbSession

from ..auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    current_user,
    revoke_session,
    set_session_cookie,
)
from ..db import get_db
from ..models import AuthState, User


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


def _surplus_base_url(request: Request) -> str:
    """Base URL the user's browser sees us at — used to construct redirect/notify
    URLs Unipile will call back. Prefer SURPLUS_BASE_URL env (production), fall
    back to the request's own origin (local dev)."""
    env = (os.environ.get("SURPLUS_BASE_URL", "") or "").strip().rstrip("/")
    if env:
        return env
    return f"{request.url.scheme}://{request.url.netloc}"


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

@router.post("/linkedin/start")
async def linkedin_start(
    request: Request,
    db: DbSession = Depends(get_db),
) -> JSONResponse:
    dsn, api_key = _ensure_unipile_configured()

    state_token = secrets.token_urlsafe(32)
    db.add(AuthState(state_token=state_token, status="pending"))
    db.commit()

    base = _surplus_base_url(request)
    expires = (_utcnow() + timedelta(hours=1)).isoformat().replace("+00:00", ".000Z")

    body = {
        "type": "create",
        "providers": ["LINKEDIN"],
        "api_url": dsn,
        "expiresOn": expires,
        # Unipile redirects the user's browser here after the hosted flow.
        # We pass the state_token in the URL so callback can correlate.
        "success_redirect_url": f"{base}/api/auth/linkedin/callback?state={state_token}",
        "failure_redirect_url": f"{base}/signin?error=linkedin_auth_failed",
        # Webhook fires server-to-server with the new account_id.
        "notify_url": f"{base}/api/auth/linkedin/webhook",
        # The state_token is echoed back in the webhook payload as `name`.
        "name": state_token,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{dsn}/api/v1/hosted/accounts/link",
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
                json=body,
            )
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Unipile rejected hosted-auth request: {data.get('message') or r.status_code}",
                )
            return JSONResponse({"url": data.get("url"), "state_token": state_token})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Unipile: {e!r}")


# ─── 2. Webhook: Unipile tells us a new account was created ────────

async def _fetch_unipile_profile(account_id: str, dsn: str, api_key: str) -> dict:
    """Pull the connected LinkedIn profile so we can populate the User row
    with name + avatar + email. Best-effort — returns {} on failure."""
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


def _extract_profile_fields(account_data: dict) -> dict:
    """Pluck the fields we want out of Unipile's account payload, tolerating
    a few different shapes the API uses across providers/versions."""
    # Unipile typically nests LinkedIn-specific fields under params/connection_params
    # for LINKEDIN, but the top-level often has name/email/picture too.
    params = account_data.get("connection_params") or account_data.get("params") or {}
    li = params.get("im") or params.get("linkedin") or params  # forgive variations

    name = (
        account_data.get("name")
        or li.get("name")
        or " ".join(filter(None, [li.get("first_name"), li.get("last_name")]))
        or ""
    ).strip()
    return {
        "name": name,
        "email": account_data.get("email") or li.get("email"),
        "headline": li.get("headline") or li.get("occupation"),
        "avatar_url": (
            account_data.get("picture")
            or li.get("picture_url")
            or li.get("picture")
            or li.get("profile_picture_url")
        ),
        "linkedin_public_id": li.get("public_identifier") or li.get("vanityName"),
        "linkedin_provider_id": li.get("entity_urn") or li.get("provider_id") or li.get("member_urn"),
    }


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
        # Existing user re-connecting — refresh profile fields, mark active
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
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    """User's browser redirected here by Unipile after they auth'd.

    The webhook may or may not have fired by the time we get here — poll
    briefly for the AuthState to resolve before deciding.
    """
    base_redirect = "/"
    error_redirect = "/signin?error=linkedin_callback_failed"

    auth_state = db.query(AuthState).filter(AuthState.state_token == state).first()
    if not auth_state:
        return RedirectResponse(error_redirect, status_code=303)

    # Poll up to ~5s for the webhook to land if it hasn't already
    for _ in range(20):
        if auth_state.user_id is not None:
            break
        if auth_state.status == "failed":
            return RedirectResponse(error_redirect, status_code=303)
        await asyncio.sleep(0.25)
        db.refresh(auth_state)

    if auth_state.user_id is None:
        # Webhook never landed; show signin with a "still processing" hint
        return RedirectResponse("/signin?error=linkedin_pending", status_code=303)

    user = db.query(User).filter(User.id == auth_state.user_id).first()
    if not user:
        return RedirectResponse(error_redirect, status_code=303)

    sess = create_session(db, user)
    auth_state.status = "callback_done"
    auth_state.completed_at = _utcnow()
    db.commit()

    response = RedirectResponse(base_redirect, status_code=303)
    set_session_cookie(response, sess.session_token)
    return response


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
    })


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
