"""
modal_jobs.py : run surplus' LLM/batch jobs on Modal instead of inside the
Railway web dyno.

WHY
---
The web app (FastAPI on Railway) is latency-sensitive and single-purpose:
serve requests, hold a small Postgres pool. The heavy work — per-applicant
triage scoring (Haiku fan-out, concurrency 25), prospecting (multi-source web
search + Sonnet judging), and the full prospect→score→outreach pipeline — is
bursty, CPU/IO-spiky, and can run for minutes. Today that runs in a FastAPI
BackgroundTask on the same dyno, which (a) competes with request handling,
(b) dies if the dyno restarts mid-deploy, and (c) can't scale past one box.

Modal is a better home for exactly this shape of work: serverless containers
that autoscale on `.map()`, retry on crash, and bill per-second. We keep the
web app on Railway (NOT moved to Modal — the user explicitly wants the
frontend/app untouched) and offload only the batch jobs.

ARCHITECTURE
------------
    Railway web app ──spawn()──▶ Modal function ──┐
                                                  ├─▶ shared Postgres (Railway)
    `modal run` / schedule ─────▶ Modal function ─┘   shared Anthropic/Exa/Unipile

Both the web app and the Modal functions point at the SAME DATABASE_URL, so a
job that scores an event writes rows the web app immediately reads. The Modal
functions import the existing `backend/` package unchanged — no logic is
duplicated; we just call evaluate_all / run_prospect / run_pipeline from a
Modal container.

GRANULARITY
-----------
Per-EVENT, not per-applicant. The inner `_one(applicant)` closure inside
evaluate_all depends on per-event context (rubric, triage_config, priority
policy, the shared semaphore, the second-pass deferred list), so the natural
Modal unit is "score one whole event". Fan-out across many events uses
`run_triage_event.map(event_ids)` — Modal then runs each event in its own
container, in parallel, with per-container retries.

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
       UNIPILE_TRIAGE_API_KEY=... UNIPILE_TRIAGE_ACCOUNT_IDS=... \
       GITHUB_TOKEN=...

   NOTE: Modal containers can't reach Railway's *.railway.internal host, so
   DATABASE_URL here must be the PUBLIC proxy URL (DATABASE_PUBLIC_URL on the
   Postgres service), not the internal one.

3. Deploy:           modal deploy modal_jobs.py
   Run one event:    modal run modal_jobs.py::run_triage_event --event-id 1
   Fan out a sweep:  modal run modal_jobs.py::triage_sweep

TRIGGER FROM THE WEB APP
------------------------
See backend/jobs.py (thin client). With USE_MODAL=1 set on Railway, the triage
route calls `modal.Function.from_name("surplus-jobs", "run_triage_event")
.spawn(event_id)` instead of a local BackgroundTask. Without it, behaviour is
unchanged (local background task) — so this is a safe, reversible flag.
"""
from __future__ import annotations

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
    # prospect_pool.json is read at import time by agents/sources/base.py
    # (the prospecting mock pool) — without it, importing backend.pipeline
    # raises FileNotFoundError and prospect/match jobs die before they start.
    # NB: we deliberately do NOT bundle backend/data/surplus.db (the 6.6 MB
    # local-dev SQLite file) — Modal talks to Postgres via DATABASE_URL.
    .add_local_file(
        "backend/data/prospect_pool.json",
        "/root/backend/data/prospect_pool.json",
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

# Sensible ceilings. Triage scoring fans out 25-wide *inside* one container
# (asyncio.Semaphore), so a single 1-CPU container handles a 500-applicant
# event; the per-event timeout is generous because rubric synth + 500 Haiku
# calls + Judge B can take a few minutes. retries=2 covers transient
# Anthropic/Postgres blips without re-charging a whole successful run.
_TRIAGE_TIMEOUT = 60 * 30   # 30 min hard cap per event
_PROSPECT_TIMEOUT = 60 * 15


# --------------------------------------------------------------------------- #
# 1) TRIAGE SCORING — the primary batch job.
#    Mirrors backend/routes/triage.py::_evaluate_event_async exactly, but in a
#    Modal container with its own DB session.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_TRIAGE_TIMEOUT,
    retries=2,
)
async def run_triage_event(event_id: int, force_reenrich: bool = False) -> dict:
    """Score every applicant for one event. Returns {total, scored, failed}.

    Idempotent: enrichment is frozen on Applicant.enrichment_raw + the
    cross-event identity cache, and scoring is temp=0, so re-running an event
    re-scores deterministically without re-enriching (unless force_reenrich).
    """
    from backend.db import SessionLocal, init_db
    from backend import models
    from backend.triage.rubric import synthesize_rubric, icp_from_event
    from backend.triage.score import evaluate_all

    # Migrations are idempotent; cheap insurance that the Modal container's
    # view of the schema matches the web app's even if it booted first.
    init_db()

    db = SessionLocal()
    try:
        ev = db.get(models.Event, event_id)
        if ev is None:
            print(f"  [modal.triage] event={event_id} NOT FOUND")
            return {"event_id": event_id, "error": "not_found"}

        applicants = list(ev.applicants)
        if not applicants:
            print(f"  [modal.triage] event={event_id} has 0 applicants")
            return {"event_id": event_id, "total": 0, "scored": 0, "failed": 0}

        print(f"  [modal.triage] event={event_id} scoring {len(applicants)} applicants")
        rubric = synthesize_rubric(
            ev.id, ev.triage_config or "", applicants,
            icp=icp_from_event(ev),
        )
        result = await evaluate_all(
            db, ev, rubric, force_reenrich=force_reenrich
        )
        print(f"  [modal.triage] event={event_id} done: {result}")
        return {"event_id": event_id, **result}
    finally:
        db.close()


@app.function(image=image, secrets=[secret], timeout=_TRIAGE_TIMEOUT)
def triage_sweep(force_reenrich: bool = False) -> list[dict]:
    """Re-score every triage event in the DB, one container per event.

    Useful as a scheduled backfill or after a scorer change. Uses Modal's
    fan-out: each event runs in its own container, in parallel, with the
    per-event retries from run_triage_event.
    """
    from backend.db import SessionLocal
    from backend import models

    db = SessionLocal()
    try:
        # Triage events are the ones with a non-empty triage_config.
        rows = (
            db.query(models.Event.id)
            .filter(models.Event.triage_config != "")
            .all()
        )
        event_ids = [r[0] for r in rows]
    finally:
        db.close()

    print(f"  [modal.triage_sweep] fanning out over {len(event_ids)} events")
    results = list(
        run_triage_event.map(
            event_ids,
            kwargs={"force_reenrich": force_reenrich},
        )
    )
    return results


# --------------------------------------------------------------------------- #
# 2) PROSPECTING — multi-source discovery + scoring for one event.
#    Mirrors the route that calls backend/pipeline.py::run_prospect.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_PROSPECT_TIMEOUT,
    retries=1,  # web_search is non-deterministic; one retry, not two
)
async def run_prospecting(event_id: int, force_fresh: bool = False) -> dict:
    """Fan out across source adapters, persist + score prospects for one event.

    Returns {event_id, prospects, failures} counts. force_fresh busts the
    in-memory ICP cache in prospect()."""
    from backend.db import SessionLocal, init_db
    from backend import models
    from backend.pipeline import run_prospect

    init_db()
    db = SessionLocal()
    try:
        ev = db.get(models.Event, event_id)
        if ev is None:
            print(f"  [modal.prospect] event={event_id} NOT FOUND")
            return {"event_id": event_id, "error": "not_found"}

        prospects, failures = await run_prospect(db, ev, force_fresh=force_fresh)
        db.commit()
        out = {
            "event_id": event_id,
            "prospects": len(prospects),
            "failures": len(failures),
        }
        print(f"  [modal.prospect] event={event_id} done: {out}")
        return out
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) FULL PIPELINE — prospect → score → outreach compose, for one event.
#    The heaviest job; lives on Modal so a multi-minute run never ties up a
#    web worker. Mirrors backend/pipeline.py::run_pipeline.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_PROSPECT_TIMEOUT,
    retries=1,
)
async def run_full_pipeline(event_id: int) -> dict:
    """Run the end-to-end prospect+outreach pipeline for one event."""
    from backend.db import SessionLocal, init_db
    from backend import models
    from backend.pipeline import run_pipeline

    init_db()
    db = SessionLocal()
    try:
        ev = db.get(models.Event, event_id)
        if ev is None:
            return {"event_id": event_id, "error": "not_found"}
        await run_pipeline(db, ev)
        db.commit()
        print(f"  [modal.pipeline] event={event_id} done")
        return {"event_id": event_id, "ok": True}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4) ASYNC JOBS — request/response prospecting + matching keyed off a Job row.
#    Unlike run_prospecting/run_full_pipeline above (fire-and-forget, return
#    counts), these power the frontend's start+poll flow: the web app inserts a
#    queued Job, spawns one of these, and the worker writes the serialized
#    PipelineResult / MatchResult onto the Job row for the frontend to poll.
#    The actual work lives in backend/jobs.py::execute_*_job — single source of
#    truth shared with the local BackgroundTask path — so these are thin shells.
# --------------------------------------------------------------------------- #
@app.function(
    image=image,
    secrets=[secret],
    timeout=_PROSPECT_TIMEOUT,
    retries=1,
)
async def run_prospect_job(job_id: str, force_fresh: bool = False) -> None:
    """Execute a queued prospect Job (search) and persist its PipelineResult."""
    from backend.db import init_db
    from backend.jobs import execute_prospect_job

    init_db()
    await execute_prospect_job(job_id, force_fresh=force_fresh)


@app.function(
    image=image,
    secrets=[secret],
    timeout=_PROSPECT_TIMEOUT,
    retries=1,
)
async def run_match_job(job_id: str) -> None:
    """Execute a queued match Job and persist its MatchResult."""
    from backend.db import init_db
    from backend.jobs import execute_match_job

    init_db()
    await execute_match_job(job_id)


# --------------------------------------------------------------------------- #
# 5) RELATIONSHIP WATCH — poll each user's CRM (Contact spine) for LinkedIn
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
# 6) UPDATES SWEEP — the tiered "what's new" sweep (job changes + milestone
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
# Local entrypoints: `modal run modal_jobs.py::<name>`
# --------------------------------------------------------------------------- #
@app.local_entrypoint()
def main(event_id: int = 0, job: str = "triage", force: bool = False):
    """Convenience CLI.

    Examples:
      modal run modal_jobs.py --event-id 1                 # triage one event
      modal run modal_jobs.py --event-id 1 --job prospect  # prospect one event
      modal run modal_jobs.py --job sweep                  # re-score all events
    """
    if job == "sweep":
        print(triage_sweep.remote(force_reenrich=force))
    elif job == "prospect":
        print(run_prospecting.remote(event_id, force_fresh=force))
    elif job == "pipeline":
        print(run_full_pipeline.remote(event_id))
    else:
        print(run_triage_event.remote(event_id, force_reenrich=force))
