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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routes import admin, auth, curation, demo, events, pipeline, matching, roi, triage, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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

app.include_router(auth.router)
app.include_router(demo.router)
app.include_router(events.router)
app.include_router(pipeline.router)
app.include_router(matching.router)
app.include_router(roi.router)
app.include_router(triage.router)
app.include_router(curation.router)
app.include_router(webhooks.router)
app.include_router(admin.router)


# NB: previously had a verbose 500 exception handler here that leaked
# tracebacks in response bodies : used to debug the multi-tenant
# datetime bug. Removed once the bug was fixed since leaking internals
# is a security smell. If we hit another mysterious 500, add it back
# temporarily : see git blame for the exact handler.


@app.get("/api/health", tags=["meta"])
def health():
    """API discovery JSON. Moved from `/` so the frontend can own `/`."""
    return {
        "service": "surplus-roi-engine",
        "version": "0.1.0",
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
    from starlette.responses import FileResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                # Only fall back for client-side routes (404 + non-API).
                # Other status codes (405, etc.) bubble up unchanged.
                if exc.status_code == 404 and not path.startswith("api/"):
                    return FileResponse(str(_FRONTEND_DIST / "index.html"))
                raise

    app.mount("/", SPAStaticFiles(directory=str(_FRONTEND_DIST), html=True),
              name="frontend")
