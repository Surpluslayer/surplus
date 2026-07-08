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
import base64 as _b64
import hmac
import json as _json
import os
import time as _time

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


# ── Email verification (no key needed on the caller) ─────────────────────────
# Microsoft/O365 domains verify via GetCredentialType over HTTPS (works from
# Railway; more reliable than SMTP for O365, incl. catch-all/Mimecast). Non-MS
# domains (Google, etc.) can't be checked here (Railway blocks outbound :25), so
# they're flagged "unverified_non_microsoft" — the caller can SMTP-verify those
# from an open-egress machine or the GH Actions verifier. One relay token unlocks
# this, so a sandbox needs no separate GitHub PAT for the O365 majority.
_IFEXISTS = {0: "valid", 1: "invalid", 5: "valid", 6: "valid"}


@router.post("/verify-email")
async def verify_email(request: Request) -> Response:
    _check_token(request)
    try:
        data = _json.loads((await request.body()) or b"{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    emails = data.get("emails") or ([data["email"]] if data.get("email") else [])
    if not isinstance(emails, list) or not emails:
        raise HTTPException(status_code=400, detail="provide {'emails': [...]} or {'email': '...'}")

    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for email in emails[:200]:
            dom = str(email).rsplit("@", 1)[-1].strip().lower()
            verdict, method = "unknown", "none"
            try:
                realm = await client.get(
                    f"https://login.microsoftonline.com/getuserrealm.srf?login=user@{dom}&json=1")
                ns = realm.json().get("NameSpaceType") if realm.status_code == 200 else None
                if ns in ("Managed", "Federated"):
                    cred = await client.post(
                        "https://login.microsoftonline.com/common/GetCredentialType",
                        json={"Username": email},
                        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
                    ifx = cred.json().get("IfExistsResult")
                    verdict = _IFEXISTS.get(ifx, f"unknown({ifx})")
                    method = f"o365/{ns}"
                else:
                    verdict, method = "unverified_non_microsoft", "skip"
            except Exception as exc:  # noqa: BLE001 : best-effort per address
                verdict, method = f"err_{type(exc).__name__}", "error"
            results.append({"email": email, "verdict": verdict, "method": method})

    return Response(content=_json.dumps({"results": results}), media_type="application/json")


# ── Trigger the autonomous send-campaign workflow (no gh / repo-scope on caller) ─
# The caller (skill/sandbox) POSTs an approved batch here with only the relay
# token; Railway dispatches the GitHub Actions workflow using its own GitHub
# token (GITHUB_DISPATCH_TOKEN). So no caller ever needs `gh` or GitHub access.
_DISPATCH_REPO = os.environ.get("SEND_CAMPAIGN_REPO", "Surpluslayer/gtm_machine")
_DISPATCH_WORKFLOW = os.environ.get("SEND_CAMPAIGN_WORKFLOW", "send-campaign.yml")


@router.post("/trigger-send")
async def trigger_send(request: Request) -> Response:
    _check_token(request)
    gh_token = (os.environ.get("GITHUB_DISPATCH_TOKEN") or "").strip()
    if not gh_token:
        raise HTTPException(status_code=503, detail="GITHUB_DISPATCH_TOKEN not configured on server")
    try:
        data = _json.loads((await request.body()) or b"{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    recipients = data.get("recipients")
    from_account = (data.get("from_account_id") or "").strip()
    if not recipients or not from_account:
        raise HTTPException(status_code=400, detail="need {'recipients':[...], 'from_account_id':'...'}")
    b64 = _b64.b64encode(_json.dumps(recipients).encode()).decode()
    inputs = {
        "recipients_b64": b64,
        "from_account_id": from_account,
        "cc": (data.get("cc") or "").strip(),
        "dry_run": "true" if data.get("dry_run") else "false",
    }
    if data.get("subject"):
        inputs["subject"] = data["subject"]
    url = (f"https://api.github.com/repos/{_DISPATCH_REPO}/actions/"
           f"workflows/{_DISPATCH_WORKFLOW}/dispatches")
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers,
                                     json={"ref": "main", "inputs": inputs})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub dispatch failed: {exc}")
    if resp.status_code not in (204, 201, 200):
        raise HTTPException(status_code=502, detail=f"GitHub dispatch {resp.status_code}: {resp.text[:200]}")
    return Response(content=_json.dumps({"ok": True, "dispatched": len(recipients),
                                         "repo": _DISPATCH_REPO, "workflow": _DISPATCH_WORKFLOW}),
                    media_type="application/json")


# ── Schedule a send for a future time (queue -> cron fires it) ────────────────
# Appends {send_at, from, cc, subject, recipients} to scheduled_sends.json in the
# org repo. The scheduler cron (every 10 min) fires any job whose send_at has
# passed. Caller needs only the relay token; Railway writes the queue with its
# own GitHub token. send_at = ISO-8601 UTC (e.g. "2026-07-09T13:00:00Z").
@router.post("/schedule-send")
async def schedule_send(request: Request) -> Response:
    _check_token(request)
    gh_token = (os.environ.get("GITHUB_DISPATCH_TOKEN") or "").strip()
    if not gh_token:
        raise HTTPException(status_code=503, detail="GITHUB_DISPATCH_TOKEN not configured on server")
    try:
        data = _json.loads((await request.body()) or b"{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    recipients = data.get("recipients")
    frm = (data.get("from_account_id") or "").strip()
    send_at = (data.get("send_at") or "").strip()
    if not (recipients and frm and send_at):
        raise HTTPException(status_code=400, detail="need {'recipients':[...], 'from_account_id':'...', 'send_at':'ISO-UTC'}")
    job = {"id": f"job-{int(_time.time())}", "send_at": send_at, "from_account_id": frm,
           "cc": (data.get("cc") or "").strip(), "subject": data.get("subject", ""), "recipients": recipients}
    path = f"https://api.github.com/repos/{_DISPATCH_REPO}/contents/scheduled_sends.json"
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        for _attempt in range(3):
            g = await client.get(path + "?ref=main", headers=headers)
            if g.status_code == 200:
                gd = g.json(); sha = gd.get("sha")
                try:
                    cur = _json.loads(_b64.b64decode(gd.get("content", "")) or b"[]")
                    if not isinstance(cur, list): cur = []
                except Exception:
                    cur = []
            elif g.status_code == 404:
                sha = None; cur = []
            else:
                raise HTTPException(status_code=502, detail=f"read queue {g.status_code}: {g.text[:150]}")
            cur.append(job)
            put = {"message": f"schedule: queue {len(recipients)} for {send_at}",
                   "content": _b64.b64encode(_json.dumps(cur, indent=1).encode()).decode(), "branch": "main"}
            if sha:
                put["sha"] = sha
            p = await client.put(path, headers=headers, json=put)
            if p.status_code in (200, 201):
                return Response(content=_json.dumps({"ok": True, "scheduled_for": send_at,
                                                     "count": len(recipients), "id": job["id"]}),
                                media_type="application/json")
            if p.status_code == 409:  # sha stale (concurrent write) -> retry
                continue
            raise HTTPException(status_code=502, detail=f"write queue {p.status_code}: {p.text[:150]}")
    raise HTTPException(status_code=409, detail="queue busy, retry")
