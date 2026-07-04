"""routes/_oauth_login.py : shared OAuth-login flow for the per-provider login routes.

google_login and microsoft_login were ~95% identical. This holds the common logic --
consent-URL start and the callback (verify state, exchange, fetch identity,
find-or-create, mint session, web-cookie vs native-Bearer) -- parametrized
by a small `login_mod` (integrations.google_login / .microsoft_login) that supplies the
provider name + authorize_url/verify_state/exchange_code/fetch_identity.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import (create_session, find_or_create_oauth_user,
                    normalize_client, set_session_cookie)
from ..integrations import oauth
from .auth import _surplus_base_url


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_surplus_base_url(request)}/api/auth/{provider}/callback"


# Scopes that only prove identity. A grant containing nothing beyond these carries no
# DATA access, so saving a ConnectedAccount for it would falsely flip the Connections
# screen to "connected" (google_connected checks row existence) with a token that can't
# sync anything.
_IDENTITY_SCOPES = frozenset({
    "openid", "email", "profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
})


def _auto_connect(db, *, user_id: int, provider: str, email: str, tokens: dict) -> None:
    """Save a ConnectedAccount from the login tokens when the grant actually carries
    DATA scopes (Microsoft login requests mail/calendar, so it auto-connects; Google
    login is identity-only under incremental auth, so it saves nothing and the
    Connections screen offers the in-context connect instead). Best-effort: a save
    hiccup must never break sign-in."""
    try:
        granted = set((tokens.get("scope") or "").split())
        if granted and not (granted - _IDENTITY_SCOPES):
            return  # identity-only grant: nothing to connect
        if tokens.get("access_token") or tokens.get("refresh_token"):
            oauth.save_tokens(db, user_id=user_id, provider=provider,
                              account_email=email or "", tokens=tokens)
    except Exception:  # noqa: BLE001
        pass


def login_url(login_mod, request: Request, *, client: str, redirect: int):
    """Consent URL (or a 302 to it) for sign-in. 409 if the provider isn't configured."""
    if not login_mod.configured():
        raise HTTPException(409, f"{login_mod.PROVIDER} sign-in is not configured on this server")
    url = login_mod.authorize_url(
        redirect_uri=_redirect_uri(request, login_mod.PROVIDER), client=normalize_client(client))
    if redirect:
        return RedirectResponse(url, status_code=302)
    return {"url": url}


def callback(login_mod, request: Request, db, *, code, state, error):
    """OAuth redirect target: find-or-create the user and mint a session."""
    provider = login_mod.PROVIDER
    base = _surplus_base_url(request)
    if error or not code:
        return RedirectResponse(f"{base}/?login={provider}&status=denied", status_code=302)
    payload = login_mod.verify_state(state or "")
    if not payload:
        raise HTTPException(400, "invalid or expired state")
    client = normalize_client(payload.get("c"))

    tokens = login_mod.exchange_code(code=code, redirect_uri=_redirect_uri(request, provider))
    ident = login_mod.fetch_identity(tokens.get("access_token", ""))
    if not ident.get("sub"):
        raise HTTPException(400, f"{provider.capitalize()} did not return an identity")

    user = find_or_create_oauth_user(
        db, provider=provider, sub=ident["sub"], email=ident["email"], name=ident["name"])
    _auto_connect(db, user_id=user.id, provider=provider,
                  email=ident["email"], tokens=tokens)
    sess = create_session(db, user, client=client)
    if client == "web":
        resp = RedirectResponse(f"{base}/?login={provider}&status=ok", status_code=302)
        set_session_cookie(resp, sess.session_token, host=request.headers.get("host"))
        return resp
    # Native app (ios/plugin): Google/Microsoft block OAuth inside an embedded
    # WebView, so the app runs sign-in in the SYSTEM browser and we hand the
    # session token back via the surplus://auth deep link (the app then calls
    # /api/auth/mobile-adopt to set the cookie in its WebView). Same mechanism
    # the LinkedIn native flow uses. `plugin` callers that want raw JSON can pass
    # response=json.
    from urllib.parse import urlencode
    from .auth import MOBILE_REDIRECT_URI
    qs = urlencode({"token": sess.session_token})
    return RedirectResponse(f"{MOBILE_REDIRECT_URI}?{qs}", status_code=302)
