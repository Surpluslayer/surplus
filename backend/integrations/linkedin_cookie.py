"""integrations/linkedin_cookie.py : connect a LinkedIn account to Unipile from a
captured browser session cookie (the plugin's one-tap path).

The surplus browser plugin reads the user's `li_at` LinkedIn cookie (they're already
logged in) and POSTs it here; we hand it to Unipile's cookie / custom auth so the
account connects WITHOUT the slow hosted re-login. This is the server side of the
"make Unipile easy" flow.

NOTE: the exact Unipile cookie-auth payload (provider LINKEDIN + access_token=li_at) is
per their docs; verify against a live connection before relying on it. Kept fail-soft:
any error raises ValueError with a short reason the route maps to a 4xx.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .unipile_config import unipile_creds


def configured() -> bool:
    return unipile_creds() is not None


def connect_with_cookie(*, li_at: str, user_agent: str = "") -> dict:
    """POST the captured LinkedIn `li_at` cookie to Unipile to connect the account.
    Returns {"account_id": <id>, "raw": <response>}; raises ValueError on failure.
    Dedup (one User = one account) is enforced by the CALLER, which no-ops when the user
    is already actively connected, so this only runs for a new/broken connection."""
    creds = unipile_creds()
    if not creds:
        raise ValueError("Unipile not configured")
    dsn, api_key = creds
    if not (li_at or "").strip():
        raise ValueError("missing LinkedIn cookie")

    body: dict = {"provider": "LINKEDIN", "access_token": li_at.strip()}
    if user_agent:
        body["user_agent"] = user_agent
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(
                f"{dsn}/api/v1/accounts",
                headers={"X-API-KEY": api_key, "accept": "application/json",
                         "content-type": "application/json"},
                json=body)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except httpx.HTTPError as exc:
        raise ValueError(f"Unipile connect failed: {type(exc).__name__}")

    acct = (data.get("account_id") or data.get("id")
            or (data.get("account") or {}).get("id"))
    if not acct:
        raise ValueError("Unipile did not return an account id")
    return {"account_id": str(acct), "raw": data}


def delete_account(account_id: str) -> bool:
    """Best-effort remove an orphan Unipile account (a duplicate seat the dedup
    logic detected). Sync sibling of auth._delete_unipile_account, used by the
    cookie-connect route (which is a sync handler). Never raises : a failure
    just leaves the orphan in Unipile's dashboard for manual cleanup, it must
    not break or roll back the connect."""
    creds = unipile_creds()
    if not (account_id and creds):
        return False
    dsn, api_key = creds
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.delete(
                f"{dsn}/api/v1/accounts/{account_id}",
                headers={"X-API-KEY": api_key, "accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        print(f"  [linkedin_cookie.dedup.delete] account={account_id} "
              f"transport_error={type(exc).__name__}: {exc}")
        return False
    if r.status_code >= 400:
        print(f"  [linkedin_cookie.dedup.delete] account={account_id} "
              f"HTTP {r.status_code} body={r.text[:160]}")
        return False
    print(f"  [linkedin_cookie.dedup.delete] account={account_id} "
          f"deleted from Unipile")
    return True
