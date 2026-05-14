"""
main.py — FastAPI app.

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
from .routes import events, pipeline, matching, roi, webhooks


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

app.include_router(events.router)
app.include_router(pipeline.router)
app.include_router(matching.router)
app.include_router(roi.router)
app.include_router(webhooks.router)


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
    on a deployed instance — the answer here tells you whether the SDK
    can even reach Claude. Does NOT make a real `messages.create` call,
    so it doesn't cost tokens or require web_search entitlement.

    Surfaces the specific failure: DNS / TLS / refused / unreachable.
    """
    import os
    import socket
    from . import models as _models  # keep linters happy; intentional unused

    out: dict = {
        "anthropic_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "anthropic_api_key_prefix": (os.environ.get("ANTHROPIC_API_KEY") or "")[:7],
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
                                  "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
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


# --- Serve the built React frontend ---------------------------------------
# In prod (Docker build): /app/frontend/dist exists and is mounted at "/".
# Locally without a build, this branch is skipped — visit /docs for the API
# or run `cd frontend && npm run dev` for hot-reload development.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    # html=True makes StaticFiles serve index.html for "/" and for any path
    # that doesn't match an existing file (= SPA fallback).
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
