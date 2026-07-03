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

import hmac
from fastapi import FastAPI, Request, Header, Depends
from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel as _PydBase

from .db import ENGINE, init_db
from .routes import (
    # shared
    auth, google_login, microsoft_login, password_auth, account_email,
    billing, demo, webhooks, admin,
    # relationship side (the phone-first "book" / CRM)
    book, relationships, inperson, followups, integrations, messages, settings,
    # events side (the desktop event-ROI pipeline)
    events, pipeline, matching, roi, triage, curation, jobs,
    # infra: token-gated Unipile pass-through for :443-only egress sandboxes
    internal_relay,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # One-shot backfill for User rows created before the
    # _extract_profile_fields camelCase fix. Idempotent — re-runs are no-ops.
    try:
        import asyncio as _asyncio

        from .routes.auth import backfill_user_dedup_keys

        async def _backfill_quietly():
            try:
                await backfill_user_dedup_keys()
            except Exception as exc:  # noqa: BLE001
                print(f"  [startup] backfill_user_dedup_keys failed: {exc}")

        # Fire-and-forget: this makes Unipile HTTP calls, and BOOT MUST NEVER
        # WAIT ON THE NETWORK -- the healthcheck window is unforgiving and a
        # hung upstream must not keep uvicorn from accepting.
        _asyncio.get_running_loop().create_task(_backfill_quietly())
    except Exception as exc:  # noqa: BLE001
        print(f"  [startup] backfill scheduling failed: {exc}")
    # In-process updates scheduler (replaces the external GitHub-Actions cron):
    # a daemon thread that periodically runs the tiered "what's new" sweep. It's
    # claim-guarded so multiple workers/replicas don't double-fire.
    try:
        from .agents.relationship import updates_scheduler
        updates_scheduler.start()
    except Exception as exc:  # noqa: BLE001
        print(f"  [startup] updates_scheduler.start failed: {exc}")
    yield


# DB pool exhaustion (a burst of long-running background work checking out
# every connection) must degrade to a RETRIABLE 503, not a bare 500: the
# frontend's request wrapper auto-retries transient statuses, so a spike shows
# up as a ~1.5s delay instead of "Internal Server Error" (2026-07-01 incident:
# QueuePool limit reached -> /api/auth/me 500 on a phone mid-connect).
from sqlalchemy.exc import TimeoutError as _SAPoolTimeout
from fastapi.responses import JSONResponse as _JSONResponse


# Process start time (monotonic wall clock). Exposed on /api/health as
# uptime_seconds: a value that keeps resetting to near-zero across polls means
# the container is crash-looping (ON_FAILURE restarts) even while each
# individual probe returns 200 -- the silent-restart signature that a plain
# healthcheck misses. This is how the NEXT outage leaves a fingerprint.
import time as _time
_PROC_START = _time.time()


app = FastAPI(
    title="surplus · event ROI engine",
    description="AI prospecting, autonomous outreach, symbiotic matching, and "
                "verified per-guest ROI for events.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(_SAPoolTimeout)
async def _pool_exhausted_503(request: Request, exc: _SAPoolTimeout):
    return _JSONResponse(
        status_code=503,
        content={"detail": "The server is momentarily busy. Please retry."},
        headers={"Retry-After": "2", "Cache-Control": "no-store"},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One line per non-noise request (status + duration), `>>> SLOW` past 5s, and a
# traceback for any unhandled 500 -- so a user-facing error always has a logged
# cause (most "server errored" reports are slow-request client timeouts that
# otherwise log only `200 OK`). Pure-ASGI, so it never buffers streaming.
from .reqlog import RequestLogMiddleware  # noqa: E402
app.add_middleware(RequestLogMiddleware)


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

# Routers grouped by the two product sides + shared infra. See ARCHITECTURE.md
# ("Two sides" map). The grouping is organizational only — order doesn't affect
# routing (each router owns a distinct path prefix).

# ── SHARED (auth, payments, demo entry, inbound webhooks, ops) ───────────────
app.include_router(auth.router)
app.include_router(google_login.router)    # Sign in with Google (decoupled login)
app.include_router(microsoft_login.router) # Sign in with Microsoft (Outlook / 365)
app.include_router(password_auth.router)   # email + password signup / sign-in
app.include_router(account_email.router)    # email verification + password reset
app.include_router(billing.router)
app.include_router(demo.router)
app.include_router(webhooks.router)
app.include_router(admin.router)

# ── RELATIONSHIP side: the phone-first "book" / CRM (event.surpluslayer.com) ──
app.include_router(book.router)            # Today feed, drafts, ask-agent
app.include_router(relationships.router)   # contact spine, star/VIP, imports, updates
app.include_router(inperson.router)        # phone capture (QR / paste / manual)
app.include_router(messages.router)        # message capture (context in) + send queue
app.include_router(followups.router)       # scheduled follow-up queue
app.include_router(settings.router)        # per-user settings (autonomy mode)
app.include_router(integrations.router)    # OAuth source connectors (Google ...)

# ── EVENTS side: the desktop event-ROI pipeline (www.surpluslayer.com) ───────
app.include_router(events.router)          # 01 intake
app.include_router(pipeline.router)        # 02-03 prospecting + outreach
app.include_router(matching.router)        # 04 symbiotic matching
app.include_router(roi.router)             # 05 ROI ledger
app.include_router(triage.router)          # inbound applicant triage
app.include_router(curation.router)        # event attendee curation
app.include_router(jobs.router)            # async job dispatch + poll
app.include_router(internal_relay.router)  # token-gated Unipile relay (sandbox egress)


# NB: previously had a verbose 500 exception handler here that leaked
# tracebacks in response bodies : used to debug the multi-tenant
# datetime bug. Removed once the bug was fixed since leaking internals
# is a security smell. If we hit another mysterious 500, add it back
# temporarily : see git blame for the exact handler.


# Frontend fingerprint : which BookApp bundle is actually baked into THIS image,
# and whether it's the redesigned one. Lets you confirm from /api/health whether
# the new UI shipped — independent of build_time (which moves on a backend-only
# rebuild) — without loading the page or eyeballing the UI. "bk-conn-row" is a
# redesign-only CSS class that survives minification (string literal). Computed
# once and cached : the bundle can't change during a container's life, so the
# healthcheck stays cheap.
_FRONTEND_FP = None


def _frontend_fingerprint() -> dict:
    global _FRONTEND_FP
    if _FRONTEND_FP is not None:
        return _FRONTEND_FP
    info = {"book_bundle": None, "has_redesign": None}
    try:
        assets = sorted((_FRONTEND_DIST / "assets").glob("BookApp-*.js"))
        if assets:
            info["book_bundle"] = assets[0].name
            info["has_redesign"] = "bk-conn-row" in assets[0].read_text(errors="ignore")
    except Exception:
        pass
    _FRONTEND_FP = info
    return info


_EXTENSION_PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>surplus Chrome extension — Privacy Policy</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 max-width:720px;margin:48px auto;padding:0 20px;color:#1b1e22;line-height:1.6}
 h1{font-weight:800;letter-spacing:-.03em} h2{margin-top:28px;font-size:18px}
 .upd{color:#99a0a8;font-size:14px} a{color:#2f6df6} code{background:#f1f3f6;padding:1px 5px;border-radius:5px}
</style></head><body>
<h1>surplus Chrome Extension — Privacy Policy</h1>
<p class="upd">Last updated: June 28, 2026</p>
<p>The surplus Chrome extension ("the extension") lets you view your surplus
relationship book alongside your browser and capture LinkedIn profiles into your
surplus account. This policy explains what data the extension handles.</p>
<h2>What the extension accesses</h2>
<ul>
<li><b>LinkedIn profile information.</b> When you are viewing a LinkedIn profile
page (<code>linkedin.com/in/...</code>), the extension reads the publicly
displayed name, headline, and profile URL so it can show you who you are viewing
and, if you choose, capture them into surplus.</li>
<li><b>Your surplus session.</b> The extension loads your surplus book
(<code>event.surpluslayer.com</code>) in a side panel using the session cookie
already set when you signed in to surplus. The extension never sees or stores
your password.</li>
</ul>
<h2>What the extension sends, and when</h2>
<ul>
<li>A LinkedIn profile is sent to surplus <b>only when you click "Capture to
surplus."</b> At that point the name, headline, and URL are sent to your own
surplus account to create a contact and draft a message.</li>
<li>The extension does <b>not</b> continuously upload or track your browsing, and
does <b>not</b> send data to any third party other than your surplus account.</li>
</ul>
<h2>Storage</h2>
<p>The extension keeps only the most recently viewed profile in memory to
populate the panel. Captured contacts live in your surplus account, governed by
the surplus privacy policy.</p>
<h2>Data sharing and sale</h2>
<p>We do <b>not</b> sell your data or share it with advertisers or third parties.
Captured data goes only to your surplus account.</p>
<h2>Permissions, and why</h2>
<ul>
<li><b>linkedin.com</b> — read the profile you are viewing.</li>
<li><b>event.surpluslayer.com</b> — display your book and capture profiles.</li>
<li><b>side panel, tabs, scripting, storage</b> — show the panel, detect the
active LinkedIn page, and inject the profile reader.</li>
</ul>
<h2>Contact</h2>
<p>Questions: <a href="mailto:support@surpluslayer.com">support@surpluslayer.com</a></p>
</body></html>"""


@app.get("/extension-privacy", include_in_schema=False)
def extension_privacy():
    """Public privacy policy for the surplus Chrome extension (Web Store req)."""
    return HTMLResponse(_EXTENSION_PRIVACY_HTML)


# --- Marketing landing page (join.surpluslayer.com) -----------------------
# Ported in-app from the old standalone roi-engine FastAPI service, whose
# Postgres dependency at startup made the whole site 502 when the DB blipped.
# The landing is a self-contained static HTML file plus a handful of assets,
# so it has ZERO database dependency : it is a pure static serve.
#
# Host routing (see _shell_for_host below): join.* -> this landing;
# event.*/INPERSON_HOSTS -> inperson shell; www / apex -> the React SPA.
# A host-independent preview path (/landing, alias /join) is always available
# so the page can be verified on staging before the join.* domain is moved.
#
# Assets live under backend/landing/ and are served at /landing-assets/* :
# the copied join.html had its original `/static/...` references rewritten to
# `/landing-assets/...` to match this mount.
from starlette.responses import FileResponse as _FileResponse  # noqa: E402

_LANDING_DIR = Path(__file__).resolve().parent / "landing"
_LANDING_HTML = _LANDING_DIR / "join.html"


def _landing_response():
    """The marketing landing page. Pure file serve : no DB, no auth, no SPA."""
    resp = _FileResponse(str(_LANDING_HTML), media_type="text/html")
    # Shell-style no-store so a domain/content change is picked up immediately;
    # the hashed-by-name assets under /landing-assets stay cacheable.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    return resp


if _LANDING_DIR.is_dir():
    # Serve the landing's own assets (logo, design tokens, press logos, the
    # how-team images). Mounted at /landing-assets to match the rewritten
    # references inside the copied join.html.
    app.mount(
        "/landing-assets",
        StaticFiles(directory=str(_LANDING_DIR)),
        name="landing-assets",
    )

    @app.get("/landing", include_in_schema=False)
    @app.get("/join", include_in_schema=False)
    def landing_preview():
        """Host-independent preview of the marketing landing. Lets you verify
        the page (and its assets / CTA) on any host - e.g. staging - before
        the join.surpluslayer.com custom domain is repointed at this service."""
        return _landing_response()


class _DemoRequest(_PydBase):
    email: str


@app.post("/api/join/demo-request", include_in_schema=False)
def join_demo_request(payload: _DemoRequest):
    """Secondary email-capture from the landing hero, DB-FREE by design.

    The old roi-engine version persisted leads to Postgres and sent a notify
    email; that DB write is exactly the startup fragility we are removing, so
    the in-app landing must not touch the database. We validate + log the work
    email (so it shows up in request logs) and return 200 so the hero form's
    "Thanks" toast fires. The primary conversion path is the LinkedIn CTA,
    which 303s straight into /api/auth/linkedin/start-redirect."""
    email = (payload.email or "").strip().lower()
    if not email or "@" not in email or len(email) > 320:
        from fastapi import HTTPException
        raise HTTPException(400, "A valid work email is required.")
    print(f"  [landing] demo-request from {email}")
    return {"ok": True}


@app.get("/api/health", tags=["meta"])
def health(deep: bool = False,
           x_admin_token: str = Header(default=None, alias="X-Admin-Token")):
    """API discovery JSON. Moved from `/` so the frontend can own `/`.

    Railway's healthcheck hits this on an interval and RESTARTS the container if
    it fails — so the default response must be cheap and must NOT touch the DB
    pool. Under load, a DB-probing healthcheck can fail on pool exhaustion and
    trigger a restart loop that drops every in-flight request (looks like a hard
    crash). The DB/integration probe (MAX(paid_at), pending count) only runs
    with `?deep=1` for manual inspection; the platform healthcheck stays cheap.

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
    # Build stamp baked into the image by the Dockerfile. Unlike git_sha (which
    # is "unknown" unless a build-arg / RAILWAY_GIT_COMMIT_SHA is passed), this
    # refreshes whenever the copied frontend/backend layers actually rebuild —
    # so a stale deploy that silently serves an old bundle is visible at a glance
    # (the value won't have moved since the last real rebuild).
    try:
        build_time = (Path(__file__).resolve().parent / ".build_time").read_text().strip()
    except Exception:
        build_time = "unknown"
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
    # Deep diagnostics (pool/memory/db_ping/config warnings) are operator-only:
    # they reveal which integrations are configured and internal pressure. Gate
    # behind the admin token so ?deep=1 is not an unauthenticated info leak.
    if deep:
        _exp = (os.environ.get("ADMIN_TOKEN") or "").strip()
        deep = bool(_exp and x_admin_token
                    and hmac.compare_digest(x_admin_token, _exp))
    db_pool_stats = None
    mem_stats = None
    db_ping_ms = None
    warnings: list[str] = []  # leading indicators: things to fix BEFORE they page
    if deep:
        # Only on explicit ?deep=1 : never on the platform healthcheck path, so
        # DB-pool exhaustion can't fail the healthcheck and trigger a restart.
        # Pool pressure gauge: watch checked_out approach size+overflow BEFORE
        # requests start 503ing (the 2026-07-01 exhaustion gave no warning).
        try:
            _pool = ENGINE.pool
            _out, _cap = _pool.checkedout(), _pool.size() + _pool.overflow()
            _pct = round(100 * _out / _cap, 1) if _cap else 0.0
            db_pool_stats = {"size": _pool.size(), "checked_out": _out,
                             "overflow": _pool.overflow(),
                             "capacity": _cap, "used_pct": _pct}
            if _pct >= 80:
                warnings.append(f"db_pool {_pct}% used ({_out}/{_cap}) -- exhaustion imminent")
        except Exception:  # noqa: BLE001 -- SQLite/NullPool lacks these
            db_pool_stats = None

        # Container memory headroom (cgroup v2). OOM at WEB_CONCURRENCY=1 with
        # heavy imports is a silent-restart cause a 200-check misses entirely.
        try:
            cur = int(open("/sys/fs/cgroup/memory.current").read().strip())
            mx_raw = open("/sys/fs/cgroup/memory.max").read().strip()
            mx = int(mx_raw) if mx_raw != "max" else 0
            mpct = round(100 * cur / mx, 1) if mx else None
            mem_stats = {"used_mb": round(cur / 1e6, 1),
                         "limit_mb": round(mx / 1e6, 1) if mx else None,
                         "used_pct": mpct}
            if mpct is not None and mpct >= 85:
                warnings.append(f"memory {mpct}% of limit -- OOM restart risk")
        except Exception:  # noqa: BLE001 -- non-linux / no cgroup
            mem_stats = None

        # DB round-trip latency: a climbing number is the early tell of the
        # flaky Railway PG proxy or a saturated DB before it starts erroring.
        try:
            from sqlalchemy import text as _t
            # Warm the pool first so we time QUERY latency, not the one-time
            # connect+TLS+auth to the cross-region Railway PG proxy (that setup
            # alone is ~600ms and is not a health signal). Threshold is generous
            # (1500ms) because a leading indicator should flag a DB that is
            # REALLY degrading, not normal cross-region latency (a 500ms warn
            # false-alarmed on the very first real monitor run, 2026-07-03).
            with ENGINE.connect() as _c:
                _c.execute(_t("SELECT 1"))              # warm
                _t0 = _time.time()
                _c.execute(_t("SELECT 1"))              # measured
                db_ping_ms = round((_time.time() - _t0) * 1000, 1)
            if db_ping_ms >= 1500:
                warnings.append(f"db_ping {db_ping_ms}ms -- DB slow/degrading")
        except Exception as _e:  # noqa: BLE001
            warnings.append(f"db_ping FAILED: {type(_e).__name__}")

        # Config drift guard: a required prod secret going missing (PORT did,
        # 2026-07-03) is a leading indicator of an outage. Surface it here so
        # the monitor can alert BEFORE the missing value breaks a flow.
        for _var in ("DATABASE_URL", "SURPLUS_OAUTH_STATE_SECRET",
                     "UNIPILE_DSN", "UNIPILE_API_KEY", "ANTHROPIC_API_KEY"):
            if not (os.environ.get(_var) or "").strip():
                warnings.append(f"required config missing: {_var}")
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
            # Don't fail the probe on a DB blip ; surface it instead.
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
        # Baked at image build (Dockerfile). Moves on every real rebuild, so a
        # value that hasn't changed after a deploy means the build was a full
        # cache hit / stale source — i.e. your new code did NOT ship.
        "build_time": build_time,
        "uptime_seconds": round(_time.time() - _PROC_START, 1),
        # Which BookApp bundle is in this image + whether it's the redesign.
        # frontend_has_redesign==false with a fresh build_time => the BACKEND
        # rebuilt but the FRONTEND stage was served from cache (stale dist).
        "frontend_book_bundle": _frontend_fingerprint()["book_bundle"],
        "frontend_has_redesign": _frontend_fingerprint()["has_redesign"],
        # Fly stamps this per deploy even without a build-arg, so a changed
        # value confirms a fresh deploy landed even if GIT_SHA wasn't passed.
        "image_ref": os.environ.get("FLY_IMAGE_REF"),
        "db_dialect": db_dialect,
        "db_pool": db_pool_stats,
        "memory": mem_stats,
        "db_ping_ms": db_ping_ms,
        "warnings": warnings,  # non-empty => fix before it becomes an outage
        "db_url_set": bool((os.environ.get("DATABASE_URL") or "").strip()),
        "integrations": integrations,
        "last_paid_at": last_webhook_paid_at,
        "pending_replies": pending_replies_count,
        "outreach_kill_switch": kill_switch_engaged,
        "stages": ["01 intake", "02-03 pipeline", "04 matching", "05 roi"],
        "docs": "/docs",
    }



def __admin_dep(x_admin_token=Header(default=None, alias="X-Admin-Token")):
    """Gate the operator diagnostics endpoints (billed upstream calls) behind
    the admin token, 404-on-miss like routes/admin. Unauthenticated access was
    a cost-DoS + integration-config disclosure (security review H-2)."""
    from .routes.admin import _require_admin_token
    return _require_admin_token(x_admin_token)


@app.get("/api/diagnostics/anthropic", tags=["meta"])
def anthropic_diagnostics(_: None = Depends(__admin_dep)):
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
    _: None = Depends(__admin_dep),
    source: str = "linkedin",
    role: str = "ML platform engineer",
    seniority: str = "Senior",
    co_stage: str = "Seed",
    city: str = "",
    max_candidates: int = 10,
):
    """
    Probe Exa discovery directly for any source + ICP combo and return the
    raw parsed candidates. Useful when /prospect feels like a black box :
    this is the exact list our SourceAdapter would feed into the merge.

    `city` threads through exactly as the real pipeline does : it enters the
    query AND (for linkedin) the `includeText` hard-filter, so this probe can
    now reproduce the city-scoped empty result the pipeline hits. Leave it
    blank to run the wide, no-city query.

    Example:
        /api/diagnostics/exa/discover?source=linkedin&role=ML+engineer&seniority=Senior&city=New+York
    """
    from .agents import exa
    if source not in ("linkedin", "github", "x"):
        from fastapi import HTTPException
        raise HTTPException(400, "source must be one of: linkedin, github, x")
    icp = {"role": role, "seniority": seniority, "co_stage": co_stage}
    if city.strip():
        icp["city"] = city.strip()
    available = exa.exa_available()
    city_cfg = exa._resolve_city(icp.get("city") or "")
    query = exa._build_query(source, icp, city_cfg)
    # Run the parsed-output path the SourceAdapter uses, AND also surface
    # the raw Exa response so we can debug why parsing dropped fields.
    from .agents import llm
    # Strict single-pass Exa : what the city `includeText` hard-filter returns
    # on its own. This is the value that can come back empty for a tight ICP.
    strict = exa.discover_via_exa(source, icp, max_candidates=max_candidates) if available else []
    # Full adapter path : the exact call the SourceAdapter makes, including the
    # relaxation-retry that loosens the city filter when the strict pass is
    # empty. Comparing `strict_count` vs `count` shows the relaxation working.
    candidates = llm.discover_candidates(source, icp, max_candidates) if available else []
    raw_results = _exa_raw_results(source, icp, max_candidates) if available else []
    return {
        "exa_configured": available,
        "source": source,
        "icp": icp,
        "exa_query": query,
        "strict_count": len(strict),
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
def exa_diagnostics(_: None = Depends(__admin_dep)):
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
    #   event.surpluslayer.com         -> inperson.html  (phone-first capture)
    #
    # Vite's multi-page build emits both shells into dist/ referencing the same
    # hashed /assets. We just pick which shell to serve as the SPA root + the
    # client-side-route fallback, based on the request Host header. The host
    # set is env-overridable; the `event.` prefix is the convention so preview
    # subdomains (event.<env>.surpluslayer.com) Just Work.
    _INPERSON_HOSTS = {
        h.strip().lower()
        for h in (os.environ.get("INPERSON_HOSTS") or "event.surpluslayer.com").split(",")
        if h.strip()
    }
    _HAS_INPERSON_SHELL = (_FRONTEND_DIST / "inperson.html").is_file()

    def _host_from_scope(scope) -> str:
        """The user-facing host. Behind Cloudflare / Railway the edge rewrites
        the raw Host header to the origin's INTERNAL name (e.g.
        surplus-production.up.railway.app), which would make us serve the
        desktop shell on event.surpluslayer.com. The real host survives in
        X-Forwarded-Host (set by the proxy) and on the Origin / Referer of the
        navigation, so prefer those and fall back to Host last."""
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        # 1. X-Forwarded-Host : the proxy's record of the original Host. May be
        #    a comma list (client, proxy1, ...) : take the first.
        xfh = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if xfh:
            return xfh
        # 2. Origin / Referer : present on the SPA's own navigations.
        for key in ("origin", "referer"):
            val = headers.get(key) or ""
            if val:
                try:
                    from urllib.parse import urlsplit
                    h = urlsplit(val).hostname
                    if h:
                        return h
                except Exception:
                    pass
        # 3. Raw Host (may be the rewritten internal name).
        return headers.get("host") or ""

    def _is_landing_host(host: str) -> bool:
        """join.surpluslayer.com (and any join.* preview subdomain) serves the
        marketing landing instead of the React SPA. Checked before the SPA
        shell selection so the landing wins on those hosts."""
        h = (host or "").split(":")[0].lower()
        return h.startswith("join.")

    def _shell_for_host(host: str) -> str:
        h = (host or "").split(":")[0].lower()
        if _HAS_INPERSON_SHELL and (h in _INPERSON_HOSTS or h.startswith("event.")):
            return "inperson.html"
        return "index.html"

    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            host = _host_from_scope(scope)
            # join.* hosts: serve the standalone marketing landing for the root
            # / client-side routes. /landing-assets, /api, /events, etc. are
            # mounted ABOVE this catch-all, so the landing's own assets + the
            # LinkedIn CTA still resolve on the join.* host.
            if _is_landing_host(host) and _LANDING_HTML.is_file() and (
                path in ("", ".", "index.html")
            ):
                return _landing_response()
            shell = _shell_for_host(host)
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
                    # On a join.* host, an unknown path falls back to the
                    # landing (single-page) rather than the React shell.
                    if _is_landing_host(host) and _LANDING_HTML.is_file():
                        return _landing_response()
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
