"""
modal_jobs.py : run surplus' LLM/batch jobs on Modal instead of inside the
Railway web dyno.

WHY
---
The web app (FastAPI on Railway) is latency-sensitive and single-purpose:
serve requests, hold a small Postgres pool. The heavy relationship-side work —
the CRM refresh sweep, WhatsApp first-sync, connect-time detached seeds, the
hourly updates sweep, the investor outreach campaign — is bursty, IO-spiky,
and can run for minutes. Run in a FastAPI BackgroundTask on the same dyno it
(a) competes with request handling, (b) dies if the dyno restarts mid-deploy,
and (c) can't scale past one box.

Modal is a better home for exactly this shape of work: serverless containers
that autoscale on `.map()`, retry on crash, and bill per-second. We keep the
web app on Railway (NOT moved to Modal — the user explicitly wants the
frontend/app untouched) and offload only the batch jobs.

(The retired events-side jobs — triage scoring, prospecting, the full
prospect→score→outreach pipeline — used to live here too; they were deleted
with the events-side code removal.)

ARCHITECTURE
------------
    Railway web app ──spawn()──▶ Modal function ──┐
                                                  ├─▶ shared Postgres (Railway)
    `modal run` / schedule ─────▶ Modal function ─┘   shared Anthropic/Exa/Unipile

Both the web app and the Modal functions point at the SAME DATABASE_URL, so a
job that writes rows is immediately visible to the web app. The Modal functions
import the existing `backend/` package unchanged — no logic is duplicated; the
job bodies live in backend/jobs.py and backend/agents/relationship/*.

SETUP (one-time)
----------------
1. pip3 install modal && modal token new
2. Create the secret with every env var the jobs need (point DATABASE_URL at
   the env you want — staging proxy URL for staging, prod for prod):

     modal secret create surplus-jobs \
       DATABASE_URL='postgresql://...kodama.proxy.rlwy.net:PORT/railway' \
       ANTHROPIC_API_KEY=sk-ant-... \
       EXA_API_KEY=... \
       UNIPILE_API_KEY=... UNIPILE_DSN=... UNIPILE_ACCOUNT_ID=... \
       GITHUB_TOKEN=...

   NOTE: Modal containers can't reach Railway's *.railway.internal host, so
   DATABASE_URL here must be the PUBLIC proxy URL (DATABASE_PUBLIC_URL on the
   Postgres service), not the internal one.

3. Deploy:           modal deploy modal_jobs.py

TRIGGER FROM THE WEB APP
------------------------
See backend/jobs.py (thin client). With USE_MODAL=1 set on Railway, dispatch
sites call `modal.Function.from_name("surplus-jobs", ...).spawn(...)` instead
of a local BackgroundTask/thread. Without it, behaviour is unchanged (local
fallback) — so this is a safe, reversible flag.
"""
from __future__ import annotations

import os

import modal

# --------------------------------------------------------------------------- #
# Image: the backend's pinned deps + the repo source mounted as `backend/`.
# We install from requirements.txt for an exact match with Railway, then add
# the local source. anthropic/exa/unipile HTTP all happen from inside the
# container, so no extra system packages are needed.
# --------------------------------------------------------------------------- #
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    # Data assets the backend reads at import/runtime. add_local_python_source
    # mounts ONLY .py files, so non-code assets must be added explicitly.
    # NB: we deliberately do NOT bundle backend/data/surplus.db (the 6.6 MB
    # local-dev SQLite file) — Modal talks to Postgres via DATABASE_URL.
    # The curated investor roster read at runtime by
    # agents/relationship/investor_campaign.py (the batched connection-request
    # campaign). It's a data asset, not .py, so it needs an explicit mount.
    .add_local_file(
        "backend/data/investor_outreach.json",
        "/root/backend/data/investor_outreach.json",
    )
    # The jobs talk to Exa/Unipile over plain HTTPS via `requests`/SDKs that
    # are already in requirements.txt. Add the backend package last so code
    # edits don't bust the (slow) pip layer.
    .add_local_python_source("backend")
)

# Every secret value listed in SETUP step 2 lands as an env var inside the
# container, which is exactly how backend/* reads its config (os.environ).
secret = modal.Secret.from_name("surplus-jobs")

app = modal.App("surplus-jobs")



# --------------------------------------------------------------------------- #
# 1) RELATIONSHIP WATCH — poll each user's CRM (Contact spine) for LinkedIn
#    changes and emit activity_update interactions. There is NO Unipile push for
#    a tracked person's own posts/job changes (webhooks only fire for the
#    connected account's own activity), so freshness comes from POLLING on a
#    schedule. The work lives in backend/jobs.py::execute_crm_refresh (shared
#    with the manual POST /api/relationships/refresh route) — thin shell here.
# --------------------------------------------------------------------------- #
_CRM_TIMEOUT = 60 * 20


@app.function(
    image=image,
    secrets=[secret],
    timeout=_CRM_TIMEOUT,
    retries=1,  # LinkedIn reads are non-deterministic; one retry, not two
)
def run_crm_refresh(user_id: int, limit: int | None = None) -> dict:
    """Poll one user's CRM for LinkedIn changes. Returns {user_id, polled,
    changes}. Read-only against LinkedIn; best-effort per contact."""
    from backend.db import init_db
    from backend.jobs import execute_crm_refresh

    init_db()
    return execute_crm_refresh(user_id, limit=limit)


@app.function(
    image=image,
    secrets=[secret],
    timeout=_CRM_TIMEOUT,
    schedule=modal.Period(days=1),
)
def crm_refresh_sweep() -> list[dict]:
    """Daily: refresh every user's CRM, one container per user (fan-out with
    per-user retries). Scheduled — Modal fires this on modal.Period(days=1)
    once deployed; no Railway cron needed."""
    from backend.db import SessionLocal, init_db
    from backend import models

    init_db()
    db = SessionLocal()
    try:
        rows = db.query(models.User.id).all()
        user_ids = [r[0] for r in rows]
    finally:
        db.close()

    print(f"  [modal.crm_sweep] fanning out over {len(user_ids)} users")
    return list(run_crm_refresh.map(user_ids))


# --------------------------------------------------------------------------- #
# 2) WHATSAPP FIRST SYNC : the on-connect conversation import.
#    When a user connects WhatsApp, the webhook kicks a first sync that pages
#    the account's chats and ingests each conversation. That's minutes of
#    Unipile I/O, so it can't live in the request lifecycle (a throwaway thread
#    inside the webhook dies when the worker recycles -> user gets 0 convos).
#    The web app spawns THIS off the webhook (USE_MODAL on); it survives the ack,
#    autoscales, and retries once. The work lives in
#    backend/jobs.py::execute_whatsapp_first_sync (shared with the local-thread
#    fallback) -- thin shell here. Idempotent (ingest skips by message id).
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_CRM_TIMEOUT,
    retries=1,  # WhatsApp reads are non-deterministic; one retry, not two
)
def run_whatsapp_first_sync(user_id: int) -> dict:
    """Run a user's first WhatsApp conversation sync. Returns the stats dict.
    Read-only against WhatsApp; best-effort per chat."""
    from backend.db import init_db
    from backend.jobs import execute_whatsapp_first_sync

    init_db()
    return execute_whatsapp_first_sync(user_id)


# --------------------------------------------------------------------------- #
# 3) DETACHED JOB : the durable home for the generic fire-and-forget seeds
#    (connect-time conversation autoimport + voice sync, email first-sync). The
#    web app would otherwise run these in a throwaway daemon thread that dies if
#    the worker recycles mid-deploy, dropping the seed. run_detached(prefer_modal
#    =True) spawns THIS so the seed survives the ack, autoscales, and retries
#    once. The body lives in backend/jobs.py::execute_detached (shared with the
#    local-thread fallback) -- thin shell here. The seed fns are idempotent.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_CRM_TIMEOUT,
    retries=1,
)
def run_detached_job(fn_path: str, args: list, kwargs: dict) -> None:
    """Run a detached job body (fn imported from fn_path) on its own DB session."""
    from backend.db import init_db
    from backend.jobs import execute_detached

    init_db()
    execute_detached(fn_path, *(args or []), **(kwargs or {}))


# --------------------------------------------------------------------------- #
# 4) UPDATES SWEEP — the tiered "what's new" sweep (job changes + milestone
#    posts) for the Book contact spine. Primary scheduler. Bright Data scrapes
#    on its own infra and delivers to the Railway webhook; this function just
#    selects DUE contacts (vip = daily, others = weekly) and fires the triggers.
#    Shares the `scheduler_claims` DB row with the in-process thread
#    (backend/agents/updates_scheduler), so exactly one of them runs each hour —
#    Modal primary, in-process fallback. No Railway cron, no GitHub Actions.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    # surplus-jobs supplies DATABASE_URL/ANTHROPIC/etc; surplus-brightdata adds
    # the BRIGHTDATA_* vars so this Modal container can run the Bright Data path
    # (not just Exa). Kept as a SEPARATE secret so we never clobber surplus-jobs.
    secrets=[secret, modal.Secret.from_name("surplus-brightdata")],
    timeout=60 * 15,
    schedule=modal.Period(hours=1),
)
def updates_sweep() -> dict:
    """Hourly: claim + run the due-contact updates sweep. The claim guard means a
    frequent schedule never scrapes anyone beyond their tier; it only lowers the
    lag between 'became due' and 'checked'. Returns the tick status dict."""
    from backend.db import init_db
    from backend.agents.relationship import updates_scheduler
    from backend.providers import brightdata

    # Only take over as primary once Bright Data is configured in THIS (Modal)
    # env -- otherwise a Modal-run sweep would fall back to Exa, and since Modal
    # races the in-process thread for the shared claim, behavior would be
    # nondeterministic. Until the surplus-jobs secret has the BRIGHTDATA_* vars,
    # defer (don't claim) so Railway's in-process thread stays primary.
    if not brightdata.configured():
        msg = "brightdata not configured in modal secret; deferring to in-process"
        print(f"  [modal.updates_sweep] {msg}")
        return {"ran": False, "reason": msg}
    init_db()
    return updates_scheduler.run_claimed_sweep()


# --------------------------------------------------------------------------- #
# 5) INVESTOR OUTREACH : the batched, throttled LinkedIn connection campaign.
#    Sends a small daily batch of pre-written connection requests to the curated
#    investor roster (backend/data/investor_outreach.json), routed through the
#    same guarded send path as every other outreach. Spreading a fixed list over
#    many days (small cap per run) is the whole point: it keeps invite volume
#    under LinkedIn's limits and out of burst-spam territory.
#
#    DOUBLE-GATED so a deploy never fires it by accident:
#      * INVESTOR_OUTREACH_ENABLED=true  — else the daily job no-ops.
#      * UNIPILE_DRY_RUN=false           — else every send is a dry-run preview.
#    Both must be set (in the surplus-jobs secret) for real invites to go out.
#    Tune volume with INVESTOR_OUTREACH_DAILY_CAP (default 12) and pick the
#    sending account with INVESTOR_OUTREACH_USER_EMAIL.
# --------------------------------------------------------------------------- #
def _investor_outreach_enabled() -> bool:
    return (os.environ.get("INVESTOR_OUTREACH_ENABLED", "").strip().lower()
            in ("1", "true", "yes", "on"))


@app.function(
    image=image,
    secrets=[secret],
    timeout=_CRM_TIMEOUT,
    schedule=modal.Period(days=1),
)
def investor_outreach_sweep() -> dict:
    """Daily: send the next batch of investor connection requests.

    No-ops unless INVESTOR_OUTREACH_ENABLED=true. Honors UNIPILE_DRY_RUN
    (default dry-run), the daily cap, idempotency, and the confidence gate
    (auto-sends only high-confidence roster rows). Safe to leave scheduled;
    it does nothing until explicitly enabled."""
    if not _investor_outreach_enabled():
        print("  [modal.investor_outreach] disabled (INVESTOR_OUTREACH_ENABLED unset); no-op")
        return {"ran": False, "reason": "disabled"}

    from backend.db import SessionLocal, init_db
    from backend.agents.relationship import investor_campaign as ic

    init_db()
    db = SessionLocal()
    try:
        cap = None  # let run_batch read INVESTOR_OUTREACH_DAILY_CAP
        summary = ic.run_batch(db, limit=cap, high_only=True)
    finally:
        db.close()
    print(f"  [modal.investor_outreach] dry_run={summary['dry_run']} "
          f"sent={summary['sent']}/{summary['attempted']} "
          f"remaining={summary['remaining']}")
    return summary
