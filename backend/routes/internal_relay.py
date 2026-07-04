"""
routes/internal_relay.py : a token-gated pass-through to Unipile.

WHY THIS EXISTS
    Unipile's DSN is a non-standard port (api40.unipile.com:17054). Locked-down
    hosted sandboxes force outbound through a :443-only egress proxy, which
    refuses the TLS handshake on that port — so those envs cannot call Unipile
    directly no matter how correct the DSN is. Railway has open egress and the
    Unipile creds already in its env, so we let the sandbox reach Unipile
    THROUGH here: sandbox -> https://<app>/internal/unipile/... (:443, allowed)
    -> Railway -> api40.unipile.com:17054.

CONTRACT
    Anything the sandbox used to send to  {UNIPILE_DSN}/api/v1/<path>
    it now sends to                       {APP}/internal/unipile/api/v1/<path>
    with header  X-Internal-Token: <SURPLUS_INTERNAL_TOKEN>  instead of the
    X-API-KEY (this relay injects the real key server-side). Query string and
    JSON body are forwarded verbatim; the upstream status + body are returned
    unchanged. So the ONLY sandbox-side change is the base URL + that one header.

SECURITY
    - Disabled unless SURPLUS_INTERNAL_TOKEN is set (503). No token, no relay.
    - Constant-time token check; the sandbox never sees the Unipile API key.
    - Only forwards paths under api/v1/ to the configured Unipile DSN — it is
      not an open proxy to arbitrary hosts.
    - This is host-agnostic: it is mounted on every domain the service serves,
      so it works via surplus-production.up.railway.app or any custom domain.
"""
from __future__ import annotations
import hmac
import os

import httpx
from fastapi import APIRouter, Request, Response, HTTPException

router = APIRouter(prefix="/internal/unipile", tags=["internal"])

# Methods we relay. Read + the send verbs the outreach scripts use.
_ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def _unipile_dsn() -> str:
    """Same normalization as UnipileProvider: accept a bare host:port and
    prepend https://, strip trailing slash."""
    raw = (os.environ.get("UNIPILE_DSN") or "").strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


def _check_token(request: Request) -> None:
    expected = (os.environ.get("SURPLUS_INTERNAL_TOKEN") or "").strip()
    if not expected:
        # Fail closed: relay is OFF until a token is provisioned.
        raise HTTPException(status_code=503, detail="relay disabled (SURPLUS_INTERNAL_TOKEN unset)")
    got = (request.headers.get("x-internal-token") or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-Internal-Token")


@router.api_route("/{path:path}", methods=sorted(_ALLOWED_METHODS))
async def relay(path: str, request: Request) -> Response:
    _check_token(request)

    api_key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
    dsn = _unipile_dsn()
    if not (api_key and dsn):
        raise HTTPException(status_code=503, detail="UNIPILE_DSN / UNIPILE_API_KEY not configured on server")

    # Only proxy the real Unipile API surface, never an arbitrary path.
    fwd_path = path.lstrip("/")
    if not fwd_path.startswith("api/v1/"):
        raise HTTPException(status_code=403, detail="only api/v1/ paths may be relayed")

    url = f"{dsn}/{fwd_path}"
    # Forward the incoming query string verbatim (account_id, limit, cursor...).
    params = dict(request.query_params)
    body = await request.body()

    # Server-side headers: inject the real key; forward content-type if present.
    headers = {"X-API-KEY": api_key, "accept": "application/json"}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                request.method, url, params=params,
                content=body if body else None, headers=headers,
            )
    except httpx.HTTPError as exc:  # connect/timeout/reset talking to Unipile
        raise HTTPException(status_code=502, detail=f"upstream Unipile error: {type(exc).__name__}: {exc}")

    # Pass the upstream response back unchanged (status + body + content-type).
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media)
