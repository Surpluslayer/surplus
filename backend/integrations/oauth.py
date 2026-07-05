"""integrations/oauth.py : provider-agnostic OAuth 2.0 core.

authorize URL -> code exchange -> token storage -> token refresh -> "give me a valid
access token for this connected account". Pure HTTP (httpx), no provider SDKs.

State is a STATELESS HMAC-signed nonce (CSRF + replay TTL); the user is taken from
the session on the callback, so we don't need a server-side state table. The signed
state still binds the intended user_id + provider + expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .. import crypto, models
from .providers import ProviderConfig, get_provider

_STATE_TTL = 600        # signed-state lifetime (s)
_REFRESH_SKEW = 120     # refresh if the access token expires within this (s)


# ── signed state (CSRF) ───────────────────────────────────────────────────────
# The dev fallback ONLY applies when explicitly in dev (no DATABASE_URL = local
# SQLite). In prod this MUST fail closed: the same key signs OAuth CSRF state
# AND the password-reset / email-verify tokens, so a guessable fallback
# (SURPLUS_BASE_URL is public; the literal is in git) is an account-takeover
# primitive. An env var silently vanishing is a demonstrated risk on this
# service (PORT did, 2026-07-03), so we refuse to run rather than sign with junk.
def _secret() -> bytes:
    s = (os.environ.get("SURPLUS_OAUTH_STATE_SECRET") or "").strip()
    prod = bool((os.environ.get("DATABASE_URL") or "").strip())  # Postgres = prod
    if prod:
        # Prod MUST fail closed on a weak/missing secret: a guessable key lets an
        # attacker forge password-reset tokens (account takeover).
        if len(s) < 32:
            raise RuntimeError(
                "SURPLUS_OAUTH_STATE_SECRET must be set to >=32 random bytes in "
                "prod: it signs password-reset tokens, so a weak/missing value "
                "is an account-takeover primitive. Gen: secrets.token_urlsafe(48)")
        return s.encode()
    # Dev/test (no DATABASE_URL): honor an explicit secret of any length, else
    # the local literal. Never the public SURPLUS_BASE_URL fallback.
    return (s or "surplus-dev-state-secret").encode()


def sign_state(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_state(state: str) -> Optional[dict]:
    """Return the payload if the signature is valid and unexpired, else None."""
    try:
        body, sig = (state or "").split(".", 1)
    except (ValueError, AttributeError):
        return None
    expect = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if float(payload.get("exp", 0)) < time.time():
        return None
    return payload


# ── credentials / config ──────────────────────────────────────────────────────
def _creds(p: ProviderConfig) -> tuple:
    return ((os.environ.get(p.client_id_env) or "").strip(),
            (os.environ.get(p.client_secret_env) or "").strip())


def configured(provider: str) -> bool:
    """True iff this provider's client_id + secret are set (so the flow can run)."""
    p = get_provider(provider)
    if not p:
        return False
    cid, secret = _creds(p)
    return bool(cid and secret)


# ── the OAuth dance ───────────────────────────────────────────────────────────
def authorize_url(provider: str, *, redirect_uri: str, user_id: int) -> str:
    p = get_provider(provider)
    if not p:
        raise ValueError(f"unknown provider {provider!r}")
    cid, _ = _creds(p)
    state = sign_state({"u": user_id, "p": p.name, "exp": time.time() + _STATE_TTL})
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(p.scopes),
        "state": state,
        **p.extra_auth_params,
    }
    return p.auth_url + "?" + urllib.parse.urlencode(params)


def _token_request(p, data: dict) -> dict:
    """POST to a provider's token endpoint, honoring token_auth: 'basic' sends the
    client creds as an HTTP Basic header (Zoom); 'body' puts them in the form."""
    cid, secret = _creds(p)
    if getattr(p, "token_auth", "body") == "basic":
        import base64
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = httpx.post(p.token_url, data=data,
                       headers={"Authorization": f"Basic {basic}"}, timeout=20)
    else:
        r = httpx.post(p.token_url, data={**data, "client_id": cid,
                                          "client_secret": secret}, timeout=20)
    r.raise_for_status()
    return r.json()


def exchange_code(provider: str, *, code: str, redirect_uri: str) -> dict:
    return _token_request(get_provider(provider), {
        "code": code, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"})


def refresh_access_token(provider: str, *, refresh_token: str) -> dict:
    return _token_request(get_provider(provider), {
        "refresh_token": refresh_token, "grant_type": "refresh_token"})


def fetch_account_email(provider: str, access_token: str) -> str:
    """Best-effort label for the connected account (the user's own email)."""
    p = get_provider(provider)
    if not p or not p.userinfo_url:
        return ""
    try:
        r = httpx.get(p.userinfo_url,
                      headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        r.raise_for_status()
        j = r.json()
        # Google -> `email`; Microsoft Graph /me -> `mail`/`userPrincipalName`;
        # Calendly /users/me -> nested under `resource.email`.
        return (j.get("email") or j.get("mail") or j.get("userPrincipalName")
                or (j.get("resource") or {}).get("email") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


# ── token storage + refresh ───────────────────────────────────────────────────
def _expiry_from(tokens: dict) -> datetime:
    secs = int(tokens.get("expires_in", 3600) or 3600)
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


def save_tokens(db, *, user_id: int, provider: str, account_email: str,
                tokens: dict, commit: bool = True):
    """Upsert the ConnectedAccount for (user, provider, account_email). Preserves an
    existing refresh_token if the new grant omits it -- Google only returns the
    refresh_token on the FIRST consent, so re-auth must not wipe it."""
    row = (db.query(models.ConnectedAccount)
           .filter_by(user_id=user_id, provider=provider, account_email=account_email)
           .one_or_none())
    if row is None:
        row = models.ConnectedAccount(user_id=user_id, provider=provider,
                                      account_email=account_email)
        db.add(row)
    # Encrypt at rest with the tenant's DEK (no-op pass-through until a KEK is
    # configured). tenant == user_id in v1.
    if tokens.get("access_token"):
        row.access_token = crypto.encrypt_for(user_id, tokens["access_token"], db)
    if tokens.get("refresh_token"):
        row.refresh_token = crypto.encrypt_for(user_id, tokens["refresh_token"], db)
    row.token_expiry = _expiry_from(tokens)
    if tokens.get("scope"):
        row.scopes = tokens["scope"]
    row.status = "active"
    if commit:
        db.commit()
    return row


def _is_expired(row, *, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    exp = row.token_expiry
    if exp is None:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= now + timedelta(seconds=_REFRESH_SKEW)


def get_valid_access_token(db, row, *, now: Optional[datetime] = None,
                           commit: bool = True) -> Optional[str]:
    """A non-expired access token for a ConnectedAccount, refreshing via the
    refresh_token when needed (and persisting it). None when it can't be refreshed
    (no refresh_token / provider error) -> the caller treats the account as needing
    reconnection (status flips to 'error')."""
    # Tokens are encrypted at rest (per-tenant DEK); decrypt for use. Legacy
    # plaintext rows pass through unchanged (see crypto.decrypt_for).
    if not _is_expired(row, now=now):
        return crypto.decrypt_for(row.user_id, row.access_token, db) or None
    refresh = crypto.decrypt_for(row.user_id, row.refresh_token, db)
    if not refresh:
        row.status = "error"
        if commit:
            db.commit()
        return None
    try:
        tokens = refresh_access_token(row.provider, refresh_token=refresh)
    except Exception:  # noqa: BLE001 : a refresh failure must not crash the caller
        row.status = "error"
        if commit:
            db.commit()
        return None
    access = tokens.get("access_token")
    if access:
        row.access_token = crypto.encrypt_for(row.user_id, access, db)
    row.token_expiry = _expiry_from(tokens)
    row.status = "active"
    if commit:
        db.commit()
    return access or None
