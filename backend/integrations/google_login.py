"""integrations/google_login.py : Sign in with Google (the DECOUPLED login).

This is IDENTITY only -- distinct from the Google DATA connector (integrations/google
+ ConnectedAccount, which pulls Gmail/Calendar). Minimal scopes (openid email profile),
its own redirect + signed state, and it find-or-creates a User keyed on the Google
`sub`. NO Unipile required: a brand-new user gets a surplus account + session without
the slow LinkedIn hosted-auth, then connects LinkedIn later via the plugin/Unipile.

Reuses the same GOOGLE_CLIENT_ID/SECRET as the data connector (one Google app) and the
HMAC-signed state from integrations.oauth, so there's nothing new to register.
"""
from __future__ import annotations

import os
import time
import urllib.parse

import httpx

from . import oauth

_STATE_TTL = 600   # seconds a sign-in state stays valid (matches the connect flow)

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_SCOPES = "openid email profile"


def _client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def configured() -> bool:
    return bool(_client_id() and _client_secret())


def authorize_url(*, redirect_uri: str, client: str = "web",
                  intent: str = "login", user_id: int = 0) -> str:
    """Consent URL. State carries the client (web/ios/plugin) and the intent: "login"
    (default, find-or-create + session) or "link" (attach this provider to the already
    signed-in user_id -- the safe migration). user_id is embedded only for link."""
    payload = {"k": "login", "p": "google", "c": client,
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
        # online + select_account: login doesn't need a refresh token, and we want
        # the account chooser rather than silent reuse.
        "access_type": "online",
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return _AUTH + "?" + urllib.parse.urlencode(params)


def verify_state(state: str) -> dict:
    """Return the signed-state payload iff it's a valid google login state, else {}."""
    payload = oauth.verify_state(state)
    if not payload or payload.get("k") != "login" or payload.get("p") != "google":
        return {}
    return payload


def exchange_code(*, code: str, redirect_uri: str) -> dict:
    r = httpx.post(_TOKEN, data={
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_identity(access_token: str) -> dict:
    """{sub, email, name, picture} from Google's userinfo endpoint. `sub` is the
    stable per-user id we key the User on (email can change; sub doesn't)."""
    r = httpx.get(_USERINFO, headers={"Authorization": f"Bearer {access_token}"},
                  timeout=20)
    r.raise_for_status()
    d = r.json()
    return {
        "sub": (d.get("sub") or "").strip(),
        "email": (d.get("email") or "").strip().lower(),
        "name": (d.get("name") or "").strip(),
        "picture": (d.get("picture") or "").strip(),
    }
