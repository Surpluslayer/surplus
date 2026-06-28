"""integrations/granola.py : the Granola connector via its remote MCP server.

Granola has no paste-an-API-key flow for a clean UX -- instead it uses MCP-style auth:
Dynamic Client Registration (DCR) + PKCE, so the host just clicks "Connect" and
consents in the browser (no client id/secret/key). We then call its MCP tools
(query_granola_meetings / list_meetings / get_meeting_transcript / ...) over the
Streamable-HTTP JSON-RPC endpoint to pull meeting notes into the spine.

Endpoints (discovered from mcp.granola.ai/.well-known/oauth-authorization-server):
  authorize  https://mcp-auth.granola.ai/oauth2/authorize
  token      https://mcp-auth.granola.ai/oauth2/token
  register   https://mcp-auth.granola.ai/oauth2/register     (DCR)
  mcp        https://mcp.granola.ai/mcp                       (Streamable HTTP JSON-RPC)

PKCE S256 is required. The code_verifier is derived STATELESSLY as HMAC(secret, nonce)
so it survives the redirect without server storage and is never transmitted; only the
nonce travels (inside the signed state).

NOTE: the MCP wire format (session handshake) + exact tool arg/result schemas can only
be finalized against a live Granola account -- the pieces here are unit-tested with
mocks; `sync_*` lands next once a connection exists to read real shapes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Optional

import httpx

ISSUER = "https://mcp-auth.granola.ai"
AUTHORIZE_URL = f"{ISSUER}/oauth2/authorize"
TOKEN_URL = f"{ISSUER}/oauth2/token"
REGISTER_URL = f"{ISSUER}/oauth2/register"
MCP_URL = "https://mcp.granola.ai/mcp"
SCOPES = "openid email offline_access profile"

_STATE_TTL = 600
# One DCR client serves all users (registration is per-APP, not per-user); cache it
# in-process. Re-registers after a restart -- DCR is cheap + idempotent enough.
_CLIENT: dict = {}


def _secret() -> bytes:
    return (os.environ.get("SURPLUS_OAUTH_STATE_SECRET")
            or os.environ.get("SURPLUS_BASE_URL")
            or "surplus-dev-state-secret").encode()


def configured() -> bool:
    """Granola needs no pre-set creds (DCR), so it's always 'configurable'. Kept for
    parity with the OAuth providers' gate."""
    return True


# ── PKCE (stateless: verifier derived from the nonce, never transmitted) ───────
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def verifier_for(nonce: str) -> str:
    """Deterministic PKCE code_verifier for a nonce (43 chars, S256-valid)."""
    return _b64u(hmac.new(_secret(), f"pkce:{nonce}".encode(), hashlib.sha256).digest())


def challenge_for(nonce: str) -> str:
    return _b64u(hashlib.sha256(verifier_for(nonce).encode()).digest())


# ── signed state (carries user_id + nonce across the redirect) ─────────────────
def sign_state(payload: dict) -> str:
    body = _b64u(json.dumps(payload, sort_keys=True).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_state(state: str) -> Optional[dict]:
    try:
        body, sig = (state or "").split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(),
                                             hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if float(payload.get("exp", 0)) < time.time():
        return None
    return payload


# ── DCR + the OAuth dance ──────────────────────────────────────────────────────
def register_client(redirect_uri: str) -> str:
    """Dynamic Client Registration -> a client_id (cached per process). Public client
    (PKCE, no secret)."""
    if _CLIENT.get("client_id") and redirect_uri in _CLIENT.get("redirect_uris", []):
        return _CLIENT["client_id"]
    r = httpx.post(REGISTER_URL, json={
        "client_name": "surplus",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",   # public client + PKCE
        "scope": SCOPES,
    }, timeout=20)
    r.raise_for_status()
    cid = (r.json() or {}).get("client_id") or ""
    _CLIENT.update({"client_id": cid, "redirect_uris": [redirect_uri]})
    return cid


def authorize_url(*, redirect_uri: str, user_id: int) -> str:
    client_id = register_client(redirect_uri)
    nonce = _b64u(os.urandom(12))
    state = sign_state({"u": user_id, "n": nonce, "exp": time.time() + _STATE_TTL})
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPES, "state": state,
        "code_challenge": challenge_for(nonce), "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(*, code: str, redirect_uri: str, nonce: str) -> dict:
    """Trade the code for tokens, proving PKCE with the verifier recomputed from the
    nonce (no stored verifier)."""
    client_id = register_client(redirect_uri)
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "client_id": client_id,
        "code_verifier": verifier_for(nonce),
    }, timeout=20)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str, redirect_uri: str = "") -> dict:
    client_id = _CLIENT.get("client_id") or (register_client(redirect_uri) if redirect_uri else "")
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id,
    }, timeout=20)
    r.raise_for_status()
    return r.json()


# ── MCP JSON-RPC (Streamable HTTP) ─────────────────────────────────────────────
def _rpc(token: str, method: str, params: dict, *, rpc_id: int = 1,
         session_id: str = "") -> tuple:
    """One JSON-RPC call to the MCP endpoint. Returns (result_dict, session_id).
    Streamable HTTP may answer as JSON or an SSE 'data:' line -- handle both."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    r = httpx.post(MCP_URL, headers=headers, timeout=30, json={
        "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
    r.raise_for_status()
    sid = r.headers.get("Mcp-Session-Id", session_id)
    body = r.text or ""
    if "text/event-stream" in (r.headers.get("Content-Type") or ""):
        # take the last `data:` payload
        data = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = data[-1] if data else "{}"
    try:
        return (json.loads(body) or {}).get("result") or {}, sid
    except Exception:  # noqa: BLE001
        return {}, sid


def call_tool(token: str, tool: str, arguments: Optional[dict] = None) -> dict:
    """Initialize a session then invoke one MCP tool; returns its result payload."""
    _, sid = _rpc(token, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "surplus", "version": "1"}}, rpc_id=1)
    result, _ = _rpc(token, "tools/call",
                     {"name": tool, "arguments": arguments or {}},
                     rpc_id=2, session_id=sid)
    return result
