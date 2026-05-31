"""
main.py : FastAPI app.

Serves the API and (when present) the built React frontend at the same origin
so production deploys hit one URL: GET / returns the SPA, /api/* + /events/*
+ /webhooks/* serve the backend.

Run it:  uvicorn backend.main:app --reload
API docs: http://localhost:8000/docs
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path

from .env_loader import load_env

load_env()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import ENGINE, init_db
from .routes import admin, auth, billing, curation, demo, events, inperson, pipeline, matching, roi, triage, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # One-shot backfill for User rows created before the
    # _extract_profile_fields camelCase fix. Idempotent — re-runs are no-ops.
    try:
        from .routes.auth import backfill_user_dedup_keys
        await backfill_user_dedup_keys()
    except Exception as exc:  # noqa: BLE001
        # Don't let a backfill hiccup block startup; log and continue.
        print(f"  [startup] backfill_user_dedup_keys failed: {exc}")
    yield


app = FastAPI(
    title="surplus · event ROI engine",
    description="AI prospecting, autonomous outreach, symbiotic matching, and "
                "verified per-guest ROI for events.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Stamp no-store on every API response so Cloudflare (which sits in
# front of Fly and aggressively caches 404s with max-age=14400 by
# default) never caches API responses : success OR error. Without this,
# a single bad 404 during a deploy can poison an endpoint for 4 hours
# for every visitor. We can't fix this at the CF layer, so we fix it
# at the origin : Cloudflare honors `Cache-Control: no-store` and skips
# its cache when origin sends it.
#
# Covers every API path prefix the backend mounts. Anything not listed
# falls through to the SPA static files, which Vite already cache-busts
# via content-hashed filenames.
_API_PATH_PREFIXES = (
    "/api/",        # auth, demo
    "/events",      # events, pipeline, matching, roi, triage, curation
    "/admin",
    "/webhooks",
    "/docs",        # OpenAPI UI : leak risk if cached at edge
    "/openapi.json",
)

@app.middleware("http")
async def no_store_for_api(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if any(path.startswith(p) for p in _API_PATH_PREFIXES):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, private"
        )
        response.headers["Pragma"] = "no-cache"
    return response

app.include_router(auth.router)
app.include_router(demo.router)
app.include_router(events.router)
app.include_router(pipeline.router)
app.include_router(inperson.router)
app.include_router(matching.router)
app.include_router(roi.router)
app.include_router(triage.router)
app.include_router(curation.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
app.include_router(billing.router)


# NB: previously had a verbose 500 exception handler here that leaked
# tracebacks in response bodies : used to debug the multi-tenant
# datetime bug. Removed once the bug was fixed since leaking internals
# is a security smell. If we hit another mysterious 500, add it back
# temporarily : see git blame for the exact handler.


@app.get("/api/health", tags=["meta"])
def health():
    """API discovery JSON. Moved from `/` so the frontend can own `/`.

    Reports which platform served the request and the live commit, so you
    can hit www.surpluslayer.com/api/health and tell what's deployed where
    (the apex is fronted by a Cloudflare LB that can route to either origin):
      - Fly  : git_sha from the Dockerfile ARG GIT_SHA build-arg
               (`flyctl deploy --build-arg GIT_SHA=$(git rev-parse --short HEAD)`)
      - Railway : git_sha from RAILWAY_GIT_COMMIT_SHA (auto-injected, no
                  build-arg needed)
    """
    import os
    git_sha = (
        os.environ.get("GIT_SHA")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "unknown"
    )
    if os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("RAILWAY_ENVIRONMENT"):
        platform = "railway"
    elif os.environ.get("FLY_IMAGE_REF") or os.environ.get("FLY_APP_NAME"):
        platform = "fly"
    else:
        platform = "unknown"
    # DB-engine surface so we never again silently fall back to SQLite in
    # prod without noticing. Defensive : a broken ENGINE attribute access
    # must NOT 5xx this endpoint — Railway's healthcheck hits it, and a
    # 500 here causes container restart loops.
    try:
        db_dialect = ENGINE.dialect.name  # "postgresql" | "sqlite"
    except Exception:
        db_dialect = "unknown"

    # ── Tech-Week visibility : external integration health snapshot ──
    # Each is a cheap env-check or a single COUNT() ; bounded query
    # work so polling /api/health stays cheap. All wrapped in try/except
    # so any individual failure can't 5xx the healthcheck.
    def _env_bool(*names: str) -> bool:
        return any((os.environ.get(n) or "").strip() for n in names)

    integrations = {
        "anthropic_key_set":      _env_bool("ANTHROPIC_API_KEY"),
        "exa_key_set":            _env_bool("EXA_API_KEY"),
        "unipile_configured":     _env_bool("UNIPILE_DSN") and _env_bool("UNIPILE_API_KEY"),
        "stripe_secret_set":      _env_bool("STRIPE_SECRET_KEY"),
        "stripe_webhook_set":     _env_bool("STRIPE_WEBHOOK_SECRET"),
        "stripe_payment_link_set": _env_bool("STRIPE_PAYMENT_LINK"),
    }

    # Stripe-webhook freshness proxy : the most recent paid_at timestamp.
    # Tells you at a glance whether webhooks are landing. Skipped (= null)
    # when the DB query fails so the healthcheck stays a 200.
    last_webhook_paid_at = None
    pending_replies_count = None
    try:
        from sqlalchemy import text
        with ENGINE.connect() as conn:
            row = conn.execute(text(
                "SELECT MAX(paid_at) FROM users WHERE paid_at IS NOT NULL"
            )).fetchone()
            if row and row[0]:
                last_webhook_paid_at = str(row[0])
            row2 = conn.execute(text(
                "SELECT COUNT(*) FROM pending_replies WHERE status = 'pending'"
            )).fetchone()
            if row2:
                pending_replies_count = int(row2[0])
    except Exception as exc:  # noqa: BLE001
        # Don't fail the healthcheck on a DB blip ; surface it instead.
        integrations["db_probe_error"] = f"{type(exc).__name__}"

    # Kill switch — operators flip this in Railway's env to halt all
    # outreach without a redeploy. Same mechanism as
    # event_graph/messaging worker. Surfaced here so /api/health makes
    # it visible at a glance.
    kill_switch_engaged = (
        (os.environ.get("SURPLUS_KILL_OUTREACH") or "").strip().lower()
        in ("1", "true", "yes", "on")
    )

    return {
        "service": "surplus-roi-engine",
        "version": "0.1.0",
        "platform": platform,
        "git_sha": git_sha,
        # Fly stamps this per deploy even without a build-arg, so a changed
        # value confirms a fresh deploy landed even if GIT_SHA wasn't passed.
        "image_ref": os.environ.get("FLY_IMAGE_REF"),
        "db_dialect": db_dialect,
        "db_url_set": bool((os.environ.get("DATABASE_URL") or "").strip()),
        "integrations": integrations,
        "last_paid_at": last_webhook_paid_at,
        "pending_replies": pending_replies_count,
        "outreach_kill_switch": kill_switch_engaged,
        "stages": ["01 intake", "02-03 pipeline", "04 matching", "05 roi"],
        "docs": "/docs",
    }


@app.get("/api/diagnostics/anthropic", tags=["meta"])
def anthropic_diagnostics():
    """
    Tests outbound connectivity to api.anthropic.com from inside the
    container. Useful when prospecting is silently returning 0 candidates
    on a deployed instance : the answer here tells you whether the SDK
    can even reach Claude. Does NOT make a real `messages.create` call,
    so it doesn't cost tokens or require web_search entitlement.

    Surfaces the specific failure: DNS / TLS / refused / unreachable.
    """
    import os
    import socket

    raw_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    stripped_key = raw_key.strip()
    out: dict = {
        "anthropic_api_key_set": bool(stripped_key),
        "anthropic_api_key_prefix": stripped_key[:7],
        # Trailing newlines / spaces in the env var cause httpx to reject
        # the request as an "Illegal header value" before any TCP. Flag it.
        "anthropic_api_key_has_whitespace": raw_key != stripped_key,
        "https_proxy": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        "http_proxy": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
    }

    # 1. DNS
    try:
        out["dns"] = {"ok": True, "ip": socket.gethostbyname("api.anthropic.com")}
    except Exception as exc:  # noqa: BLE001
        out["dns"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return out

    # 2. TLS + HTTP via httpx (the same client the Anthropic SDK uses)
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.get("https://api.anthropic.com/v1/models",
                              headers={
                                  "x-api-key": stripped_key,
                                  "anthropic-version": "2023-06-01",
                              })
        out["http"] = {
            "ok": True,
            "status_code": resp.status_code,
            "body_preview": resp.text[:300],
        }
    except Exception as exc:  # noqa: BLE001
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        out["http"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "cause": f"{type(cause).__name__}: {cause}" if cause else None,
        }
    return out


@app.get("/api/diagnostics/exa/discover", tags=["meta"])
def exa_discover_probe(
    source: str = "linkedin",
    role: str = "ML platform engineer",
    seniority: str = "Senior",
    co_stage: str = "Seed",
    max_candidates: int = 10,
):
    """
    Probe Exa discovery directly for any source + ICP combo and return the
    raw parsed candidates. Useful when /prospect feels like a black box :
    this is the exact list our SourceAdapter would feed into the merge.

    Example:
        /api/diagnostics/exa/discover?source=linkedin&role=ML+engineer&seniority=Senior
    """
    from .agents import exa
    if source not in ("linkedin", "github", "x"):
        from fastapi import HTTPException
        raise HTTPException(400, "source must be one of: linkedin, github, x")
    icp = {"role": role, "seniority": seniority, "co_stage": co_stage}
    available = exa.exa_available()
    query = exa._build_query(source, icp)
    # Run the parsed-output path the SourceAdapter uses, AND also surface
    # the raw Exa response so we can debug why parsing dropped fields.
    candidates = exa.discover_via_exa(source, icp, max_candidates=max_candidates) if available else []
    raw_results = _exa_raw_results(source, icp, max_candidates) if available else []
    return {
        "exa_configured": available,
        "source": source,
        "icp": icp,
        "exa_query": query,
        "count": len(candidates),
        "candidates": candidates,
        "raw": raw_results,
    }


def _exa_raw_results(source: str, icp: dict, max_candidates: int) -> list:
    """Tap the same Exa request but return the raw response items (title +
    text snippet) : exposes what the parser is working with."""
    from .agents import exa as _exa
    import httpx
    query = _exa._build_query(source, icp)
    domain = {"linkedin": "linkedin.com", "github": "github.com", "x": "x.com"}[source]
    category = {"linkedin": "linkedin profile", "github": "github", "x": "tweet"}[source]
    body = {
        "query": query,
        "type": "neural",
        "category": category,
        "numResults": max(max_candidates * 3, 10),
        "includeDomains": [domain],
        "contents": {"text": True},
    }
    headers = {
        "x-api-key": _exa._api_key(),
        "content-type": "application/json",
        "accept": "application/json",
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post("https://api.exa.ai/search",
                               headers=headers, json=body)
        if resp.status_code >= 400:
            return [{"_error": f"{resp.status_code}: {resp.text[:200]}"}]
        results = (resp.json() or {}).get("results") or []
        # Trim text payload so the response stays readable
        for r in results:
            if isinstance(r.get("text"), str):
                r["text"] = r["text"][:400]
        return results
    except Exception as exc:  # noqa: BLE001
        return [{"_error": f"{type(exc).__name__}: {exc}"}]


@app.get("/api/diagnostics/exa", tags=["meta"])
def exa_diagnostics():
    """
    Tests outbound connectivity to api.exa.ai from inside the container.
    Useful when /prospect is silently returning 0 LinkedIn candidates :
    the answer here tells you whether the Exa backend can even reach
    their API and whether the key is valid.

    Does a minimal /search call (1 result, cheap) so it does cost a query
    credit. Surfaces the specific failure: DNS / TLS / 401 / 5xx.
    """
    import os
    import socket

    raw_key = os.environ.get("EXA_API_KEY") or ""
    stripped_key = raw_key.strip()
    out: dict = {
        "exa_api_key_set": bool(stripped_key),
        "exa_api_key_prefix": stripped_key[:6],
        "exa_api_key_has_whitespace": raw_key != stripped_key,
    }

    # 1. DNS
    try:
        out["dns"] = {"ok": True, "ip": socket.gethostbyname("api.exa.ai")}
    except Exception as exc:  # noqa: BLE001
        out["dns"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return out

    # 2. Minimal search to validate the key + category filter end-to-end
    if not stripped_key:
        out["http"] = {"ok": False, "error": "no key configured"}
        return out
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://api.exa.ai/search",
                headers={
                    "x-api-key": stripped_key,
                    "content-type": "application/json",
                },
                json={
                    "query": "Senior software engineer",
                    "type": "neural",
                    "category": "linkedin profile",
                    "numResults": 1,
                    "includeDomains": ["linkedin.com"],
                },
            )
        out["http"] = {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "body_preview": resp.text[:400],
        }
    except Exception as exc:  # noqa: BLE001
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        out["http"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "cause": f"{type(cause).__name__}: {cause}" if cause else None,
        }
    return out


# --- Serve the built React frontend ---------------------------------------
# In prod (Docker build): /app/frontend/dist exists and is mounted at "/".
# Locally without a build, this branch is skipped : visit /docs for the API
# or run `cd frontend && npm run dev` for hot-reload development.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    # Starlette's StaticFiles(html=True) only resolves directory indexes,
    # NOT a SPA-style catch-all fallback : /signin?error=foo 404s because
    # no `signin` file exists. We need to explicitly fall back to
    # index.html for any unknown non-/api path so React Router can pick
    # up the route on the client.
    import os
    from starlette.responses import FileResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _no_store(response):
        """Force revalidation of the SPA shell. index.html / inperson.html are
        the files Vite does not content-hash : their names are stable, and they
        reference the hashed JS/CSS bundle. If a browser or Cloudflare caches a
        shell, the app keeps loading a stale bundle after a deploy, so a fresh
        deploy never reaches the user. The hashed assets stay cacheable : only
        the shells are marked no-store."""
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"

    # ── Host-based SPA routing ───────────────────────────────────────────
    # Two front-ends ship from ONE build and ONE service, sharing the API
    # (/api, /events, /webhooks resolve the same on every host because their
    # routers are mounted above this static mount):
    #
    #   surpluslayer.com / www.        -> index.html     (the desktop pipeline)
    #   app.surpluslayer.com           -> inperson.html  (phone-first capture)
    #
    # Vite's multi-page build emits both shells into dist/ referencing the same
    # hashed /assets. We just pick which shell to serve as the SPA root + the
    # client-side-route fallback, based on the request Host header. The host
    # set is env-overridable; the `app.` prefix is the convention so preview
    # subdomains (app.<env>.surpluslayer.com) Just Work.
    _INPERSON_HOSTS = {
        h.strip().lower()
        for h in (os.environ.get("INPERSON_HOSTS") or "app.surpluslayer.com").split(",")
        if h.strip()
    }
    _HAS_INPERSON_SHELL = (_FRONTEND_DIST / "inperson.html").is_file()

    def _host_from_scope(scope) -> str:
        for k, v in scope.get("headers") or []:
            if k == b"host":
                try:
                    return v.decode("latin-1")
                except Exception:
                    return ""
        return ""

    def _shell_for_host(host: str) -> str:
        h = (host or "").split(":")[0].lower()
        if _HAS_INPERSON_SHELL and (h in _INPERSON_HOSTS or h.startswith("app.")):
            return "inperson.html"
        return "index.html"

    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            shell = _shell_for_host(_host_from_scope(scope))
            # Serve the host's shell for the root AND for any client-side route
            # (StaticFiles maps "/" -> path "" with html=True; we override so
            # the app host gets inperson.html instead of index.html).
            if path in ("", ".", "index.html"):
                resp = FileResponse(str(_FRONTEND_DIST / shell))
                _no_store(resp)
                return resp
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                # Only fall back for client-side routes (404 + non-API).
                # Other status codes (405, etc.) bubble up unchanged.
                if exc.status_code == 404 and not path.startswith("api/"):
                    resp = FileResponse(str(_FRONTEND_DIST / shell))
                    _no_store(resp)
                    return resp
                raise
            # Any HTML the mount serves is a shell : keep it fresh so deploys
            # take effect immediately. Hashed assets are untouched.
            if getattr(response, "media_type", None) == "text/html":
                _no_store(response)
            return response

    app.mount("/", SPAStaticFiles(directory=str(_FRONTEND_DIST), html=True),
              name="frontend")
