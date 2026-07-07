"""
routes/internal_relay.py : token-gated pass-throughs to Unipile and Exa.

WHY THIS EXISTS
    Locked-down hosted sandboxes force outbound through a :443-only egress
    proxy. That blocks Unipile's non-standard port (api40.unipile.com:17054),
    and we also don't want to hand a sandbox the raw UNIPILE_API_KEY / EXA_API_KEY.
    Railway has open egress and both keys already in its env, so the sandbox
    reaches these services THROUGH here over HTTPS:
        sandbox --(:443, X-Internal-Token)--> Railway --(injects real key)--> service

CONTRACT
    Unipile:  {APP}/internal/unipile/api/v1/<path>   ->  {UNIPILE_DSN}/api/v1/<path>
    Exa:      {APP}/internal/exa/<endpoint>          ->  https://api.exa.ai/<endpoint>
    Auth on both: header  X-Internal-Token: <SURPLUS_INTERNAL_TOKEN>  (the relay
    injects the real X-API-KEY / x-api-key server-side). Query + JSON body are
    forwarded verbatim; upstream status + body returned unchanged.

SECURITY
    - Disabled unless SURPLUS_INTERNAL_TOKEN is set (503). No token, no relay.
    - Constant-time token check; the sandbox never sees the Unipile or Exa keys.
    - Unipile: only api/v1/ paths. Exa: only the known search endpoints.
    - Host-agnostic: mounted on every domain the service serves.
"""
from __future__ import annotations
import hmac
import os

import httpx
from fastapi import APIRouter, Request, Response, HTTPException

router = APIRouter(prefix="/internal", tags=["internal"])

# Methods we relay. Read + the send verbs the outreach scripts use.
_ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}
# The only Exa endpoints the relay will proxy (not an open proxy to api.exa.ai).
_EXA_ENDPOINTS = {"search", "contents", "findSimilar", "answer"}


def _unipile_dsn() -> str:
    """Accept a bare host:port and prepend https://, strip trailing slash."""
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


async def _forward(method: str, url: str, params: dict, body: bytes, headers: dict) -> Response:
    """Proxy a request upstream and pass the response back unchanged."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                method, url, params=params,
                content=body if body else None, headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {type(exc).__name__}: {exc}")
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media)


@router.api_route("/unipile/{path:path}", methods=sorted(_ALLOWED_METHODS))
async def relay_unipile(path: str, request: Request) -> Response:
    _check_token(request)
    api_key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
    dsn = _unipile_dsn()
    if not (api_key and dsn):
        raise HTTPException(status_code=503, detail="UNIPILE_DSN / UNIPILE_API_KEY not configured on server")
    fwd = path.lstrip("/")
    if not fwd.startswith("api/v1/"):
        raise HTTPException(status_code=403, detail="only api/v1/ paths may be relayed")
    headers = {"X-API-KEY": api_key, "accept": "application/json"}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    return await _forward(request.method, f"{dsn}/{fwd}", dict(request.query_params), body, headers)


@router.api_route("/exa/{path:path}", methods=["GET", "POST"])
async def relay_exa(path: str, request: Request) -> Response:
    _check_token(request)
    api_key = (os.environ.get("EXA_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="EXA_API_KEY not configured on server")
    endpoint = path.strip("/")
    if endpoint not in _EXA_ENDPOINTS:
        raise HTTPException(status_code=403, detail=f"only Exa endpoints {sorted(_EXA_ENDPOINTS)} may be relayed")
    headers = {"x-api-key": api_key, "accept": "application/json"}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    return await _forward(request.method, f"https://api.exa.ai/{endpoint}",
                          dict(request.query_params), body, headers)
