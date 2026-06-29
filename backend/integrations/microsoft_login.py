"""integrations/microsoft_login.py : Sign in with Microsoft (the DECOUPLED login).

The Outlook / Microsoft 365 path -- finance runs on Outlook, and many enterprises +
.edu students are on Microsoft. Mirrors google_login: IDENTITY only (distinct from the
Outlook DATA connector), find-or-creates a User keyed on the Microsoft Graph `id`, NO
Unipile required. Uses the /common endpoint so BOTH personal Microsoft accounts
(outlook.com/hotmail) and work/school (365) accounts can sign in.

Reuses the same MICROSOFT_CLIENT_ID/SECRET as the Outlook connector (one app) and the
HMAC-signed state from integrations.oauth.
"""
from __future__ import annotations

import os
import time
import urllib.parse

import httpx

from . import oauth

PROVIDER = "microsoft"
_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
_SCOPES = "openid email profile User.Read"
_STATE_TTL = 600


def _client_id() -> str:
    return (os.environ.get("MICROSOFT_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.environ.get("MICROSOFT_CLIENT_SECRET") or "").strip()


def configured() -> bool:
    return bool(_client_id() and _client_secret())


def authorize_url(*, redirect_uri: str, client: str = "web",
                  intent: str = "login", user_id: int = 0) -> str:
    """Consent URL. intent="login" (find-or-create + session) or "link" (attach to the
    signed-in user_id -- the safe migration)."""
    payload = {"k": "login", "p": "microsoft", "c": client,
               "intent": "link" if intent == "link" else "login",
               "exp": time.time() + _STATE_TTL}
    if intent == "link" and user_id:
        payload["uid"] = int(user_id)
    state = oauth.sign_state(payload)
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "state": state,
        "response_mode": "query",
        "prompt": "select_account",
    }
    return _AUTH + "?" + urllib.parse.urlencode(params)


def verify_state(state: str) -> dict:
    """Return the signed-state payload iff it's a valid microsoft login state, else {}."""
    payload = oauth.verify_state(state)
    if not payload or payload.get("k") != "login" or payload.get("p") != "microsoft":
        return {}
    return payload


def exchange_code(*, code: str, redirect_uri: str) -> dict:
    r = httpx.post(_TOKEN, data={
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": _SCOPES,
    }, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_identity(access_token: str) -> dict:
    """{sub, email, name, picture} from Graph /me. `sub` = the stable Graph `id`; email
    falls back to userPrincipalName when `mail` is null (common for personal accounts)."""
    r = httpx.get(_GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"},
                  timeout=20)
    r.raise_for_status()
    d = r.json()
    email = (d.get("mail") or d.get("userPrincipalName") or "").strip().lower()
    return {
        "sub": (d.get("id") or "").strip(),
        "email": email,
        "name": (d.get("displayName") or "").strip(),
        "picture": "",
    }
