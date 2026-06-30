"""
jobs.py : thin dispatcher that decides WHERE an LLM/batch job runs.

Two backends:
  - "local"  : a FastAPI BackgroundTask on this dyno (the historical default).
  - "modal"  : spawn the job in a Modal container (modal_jobs.py) and return
               immediately. The job then runs off-box, autoscaled, with retries.

Selection is per-deploy via env, so flipping to Modal — or back — is a config
change, NOT a code change:

    USE_MODAL=1            -> dispatch to Modal
    (unset / 0 / false)    -> run locally (unchanged behaviour)

The Modal app is named "surplus-jobs" (see modal_jobs.py). We look functions up
by name with modal.Function.from_name and .spawn() them (fire-and-forget). The
web app never blocks on the job and never needs the `modal` package unless
USE_MODAL is on — the import is lazy.

This file intentionally has NO business logic. It only routes. The actual work
lives in backend/triage/score.py, backend/pipeline.py, etc., and is imported by
BOTH the local path (routes call it directly) and the Modal path (modal_jobs.py
imports the same functions). Single source of truth.
"""
from __future__ import annotations

import os
import uuid

_MODAL_APP = "surplus-jobs"


def use_modal() -> bool:
    """True when this deploy should offload batch jobs to Modal."""
    return (os.environ.get("USE_MODAL") or "").strip().lower() in {"1", "true", "yes"}


def _spawn_modal(function_name: str, *args, **kwargs) -> bool:
    """Spawn a Modal function by name. Returns True on success.

    Best-effort: if Modal isn't reachable (token missing, app not deployed),
    log and return False so the caller can fall back to a local task instead
    of 500-ing the request that scheduled the job.
    """
    try:
        import modal  # lazy: only needed when USE_MODAL is on
        fn = modal.Function.from_name(_MODAL_APP, function_name)
        call = fn.spawn(*args, **kwargs)
        print(f"  [jobs] spawned modal {function_name}({args},{kwargs}) "
              f"id={getattr(call, 'object_id', '?')}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [jobs] modal spawn of {function_name} FAILED "
              f"({type(exc).__name__}: {exc}); falling back to local")
        return False


def dispatch_triage(background_tasks, event_id: int, *,
                    force_reenrich: bool = False) -> str:
    """Schedule triage scoring for one event. Returns 'modal' or 'local'.

    background_tasks is the FastAPI BackgroundTasks for the local fallback.
    """
    if use_modal() and _spawn_modal(
        "run_triage_event", event_id, force_reenrich=force_reenrich
    ):
        return "modal"

    # Local fallback : the existing background-task body, unchanged.
    from .routes.triage import _evaluate_event_async
    background_tasks.add_task(
        _evaluate_event_async, event_id, force_reenrich=force_reenrich
    )
    return "local"


# --------------------------------------------------------------------------- #
# Async Job model for the heavy outbound stages (search / match).
#
# Unlike triage (fire-and-forget, no result returned to the caller), search and
# match are request/response: the frontend needs the PipelineResult / MatchResult
# back. So instead of blocking the HTTP handler, we:
#   1. create a queued Job row,
#   2. dispatch the work (Modal when USE_MODAL, else a local BackgroundTask),
#   3. hand the job id back immediately,
# and the worker writes the serialized result onto the Job row. The frontend
# polls GET .../jobs/{id} until status == done and reads result_json.
#
# execute_*_job are the single source of truth for the work; BOTH the local
# path (BackgroundTask) and the Modal path (modal_jobs.run_*_job) call them.
# --------------------------------------------------------------------------- #
def new_job(db, *, event_id: int, user_id, kind: str):
    """Insert a queued Job and return it (committed so the id is durable)."""
    from . import models
    job = models.Job(
        id=uuid.uuid4().hex,
        event_id=event_id,
        user_id=user_id,
        kind=kind,
        status="queued",
    )
    db.add(job)
    db.commit()
    return job


def _finish(db, job, *, result_json: str = "", error: str = "") -> None:
    job.status = "error" if error else "done"
    job.result_json = result_json
    job.error = error[:2000] if error else ""
    db.commit()


async def execute_prospect_job(job_id: str, *, force_fresh: bool = False) -> None:
    """Run prospecting (search) for a Job, on its own DB session. Persists the
    prospects and stores a serialized PipelineResult on the Job row."""
    from .db import SessionLocal
    from . import models, schemas
    from .pipeline import run_prospect

    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            print(f"  [jobs] prospect job {job_id} NOT FOUND")
            return
        job.status = "running"
        db.commit()
        ev = db.get(models.Event, job.event_id)
        if ev is None:
            _finish(db, job, error="event not found")
            return
        # Idempotent : wipe prior prospects (and their outreach/conversions)
        # before re-surfacing, mirroring the sync /prospect route.
        from .routes.pipeline import _wipe_prior_prospects
        _wipe_prior_prospects(db, ev)
        prospects, failures = await run_prospect(db, ev, force_fresh=force_fresh)
        result = schemas.PipelineResult.build(ev, prospects, failures=failures)
        _finish(db, job, result_json=result.model_dump_json())
        print(f"  [jobs] prospect job {job_id} done "
              f"({len(prospects)} prospects, {len(failures)} failures)")
    except Exception as exc:  # noqa: BLE001
        print(f"  [jobs] prospect job {job_id} FAILED: {type(exc).__name__}: {exc}")
        try:
            job = db.get(models.Job, job_id)
            if job is not None:
                _finish(db, job, error=f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


async def execute_match_job(job_id: str) -> None:
    """Run matching for a Job, on its own DB session. Persists edges/groups/
    sponsor matches and stores a serialized MatchResult on the Job row."""
    from .db import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            print(f"  [jobs] match job {job_id} NOT FOUND")
            return
        job.status = "running"
        db.commit()
        ev = db.get(models.Event, job.event_id)
        if ev is None:
            _finish(db, job, error="event not found")
            return
        from .routes.matching import compute_match
        result = compute_match(db, ev)  # sync; persists internally
        _finish(db, job, result_json=result.model_dump_json())
        print(f"  [jobs] match job {job_id} done")
    except Exception as exc:  # noqa: BLE001
        # HTTPException(409) (not-ready) lands here too — its .detail carries
        # the operator-facing message ("no confirmed guests : ...").
        detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
        print(f"  [jobs] match job {job_id} FAILED: {detail}")
        try:
            job = db.get(models.Job, job_id)
            if job is not None:
                _finish(db, job, error=str(detail))
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


# ─── Scale tripwire ───────────────────────────────────────────────────────
# The CRM watch polls each contact's LinkedIn, and LinkedIn throttles an
# account past ~100-150 profile views/day. A user tracking many hundreds of
# contacts can't be polled daily without either blowing that budget or
# stretching per-contact cadence so far the "what's new" feed goes stale.
# Fire an internal alert WELL BEFORE that wall (default 300 watchable
# contacts) so we add the prioritized-cadence layer before any user feels it.

def _crm_scale_threshold() -> int:
    try:
        return int(os.environ.get("CRM_CONTACT_ALERT_THRESHOLD", "300"))
    except ValueError:
        return 300


def _crm_scale_alert(user_id: int, watchable: int) -> None:
    """Loud, greppable warning when a user's watchable-contact count crosses the
    tripwire. Always logs (so it shows in Modal/server logs); also POSTs to
    CRM_ALERT_WEBHOOK_URL (Slack-shaped {"text": ...}) when that env is set, so
    it can page a channel without a code change. Best-effort: alerting must
    never break the sweep."""
    threshold = _crm_scale_threshold()
    if watchable < threshold:
        return
    msg = (f"[crm.scale] user_id={user_id} has {watchable} watchable contacts "
           f"(>= {threshold}). Daily polling will strain LinkedIn's per-account "
           f"view budget — add prioritized cadence before this user goes stale.")
    print("  WARNING " + msg)
    url = (os.environ.get("CRM_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps({"text": msg}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 (operator-set URL)
    except Exception as exc:  # noqa: BLE001
        print(f"  [crm.scale] alert webhook POST failed: "
              f"{type(exc).__name__}: {exc}")


def execute_crm_refresh(user_id: int, *, limit: int | None = None) -> dict:
    """Poll one user's CRM (Contact spine) for LinkedIn changes, on its own DB
    session. Used by the scheduled Modal sweep and the manual refresh route's
    Modal path. Read-only against LinkedIn; writes activity_update interactions.

    Returns {user_id, polled, changes}. Best-effort: a single contact's fetch
    error is recorded on that Contact and skipped inside refresh_user_crm."""
    from sqlalchemy import func

    from .db import SessionLocal
    from . import models
    from .providers import get_preview_provider
    from .agents.relationship.relationship_watch import refresh_user_crm

    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        if user is None:
            print(f"  [jobs] crm_refresh user {user_id} NOT FOUND")
            return {"user_id": user_id, "error": "user not found"}

        # Scale tripwire: how many of this user's contacts are LinkedIn-
        # pollable? Counted independent of `limit` (which only caps THIS
        # batch) so the alert reflects the real watch load, not the slice.
        watchable = (
            db.query(func.count(models.Contact.id))
            .filter(models.Contact.user_id == user_id)
            .filter(models.Contact.linkedin_url.isnot(None))
            .filter(models.Contact.linkedin_url != "")
            .scalar()
        ) or 0
        _crm_scale_alert(user_id, watchable)

        provider = get_preview_provider(user)  # read-only, never sends
        summary = refresh_user_crm(db, user_id, provider, limit=limit)
        print(f"  [jobs] crm_refresh user={user_id} "
              f"polled={summary['polled']} changes={summary['changes']}")
        return {"user_id": user_id, "polled": summary["polled"],
                "changes": summary["changes"]}
    except Exception as exc:  # noqa: BLE001
        print(f"  [jobs] crm_refresh user={user_id} FAILED: "
              f"{type(exc).__name__}: {exc}")
        return {"user_id": user_id, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


# ─── WhatsApp first-sync (durable on-connect import) ───────────────────────
# When a user connects WhatsApp, the webhook kicks a FIRST sync that pages the
# account's chats and ingests each conversation. That sync is minutes of
# Unipile I/O, so it must NOT live in the request lifecycle (a throwaway thread
# inside the webhook gets killed when the worker recycles, leaving the user with
# 0 conversations). This is the durable worker -- own DB session, resolves its
# own Unipile config, best-effort -- spawned to Modal (run_whatsapp_first_sync)
# with a local-thread fallback, exactly like execute_crm_refresh / run_crm_refresh.
#
# Idempotent: ingest_messages skips by Unipile message id, so a retry/re-run
# (Modal retries, or the fallback firing after a Modal spawn that also ran) only
# re-ingests genuinely-new messages. Read-only against WhatsApp.

def _unipile_env() -> tuple[str, str]:
    """(dsn, api_key) from env, normalized like routes.auth helpers. Empty
    strings when unset -- the sync then records 'unipile not configured'."""
    dsn = (os.environ.get("UNIPILE_DSN", "") or "").strip().rstrip("/")
    if dsn and not dsn.startswith(("http://", "https://")):
        dsn = f"https://{dsn}"
    api_key = (os.environ.get("UNIPILE_API_KEY", "") or "").strip()
    return dsn, api_key


def execute_whatsapp_first_sync(user_id: int) -> dict:
    """Run a user's first WhatsApp conversation sync on its own DB session.

    Returns the sync stats dict. Best-effort and never raises: the sync itself
    swallows account/HTTP errors into stats['error']. Used by the durable Modal
    path (run_whatsapp_first_sync) and the local BackgroundTask/thread fallback.
    """
    from .db import SessionLocal
    from . import models
    from .agents.relationship.whatsapp_sync import sync_whatsapp_contacts

    dsn, api_key = _unipile_env()
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        if user is None:
            print(f"  [jobs] whatsapp_first_sync user {user_id} NOT FOUND")
            return {"user_id": user_id, "error": "user not found"}
        stats = sync_whatsapp_contacts(db, user, dsn=dsn, api_key=api_key)
        print(f"  [jobs] whatsapp_first_sync user={user_id}: {stats}")
        return {"user_id": user_id, **stats}
    except Exception as exc:  # noqa: BLE001
        print(f"  [jobs] whatsapp_first_sync user={user_id} FAILED: "
              f"{type(exc).__name__}: {exc}")
        return {"user_id": user_id, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


def dispatch_whatsapp_first_sync(user_id: int) -> str:
    """Dispatch a user's first WhatsApp sync DURABLY, off the request lifecycle.

    Modal when USE_MODAL (and reachable) -- the job then survives the webhook
    returning, autoscales, and gets one retry. Otherwise a daemon thread on this
    process (same shape as the old in-request thread, but it owns its DB session
    via execute_whatsapp_first_sync). Returns 'modal' or 'local'. Idempotent, so
    a belt-and-suspenders double-run is safe. Never raises into the webhook."""
    if use_modal() and _spawn_modal("run_whatsapp_first_sync", user_id):
        return "modal"

    import threading
    threading.Thread(
        target=execute_whatsapp_first_sync, args=(user_id,),
        name=f"whatsapp-first-sync-{user_id}", daemon=True,
    ).start()
    return "local"


def dispatch_job(background_tasks, db, job, **kwargs) -> str:
    """Dispatch a Job's work. Modal when USE_MODAL (and reachable), else a local
    BackgroundTask. Stamps job.runner and returns 'modal' or 'local'."""
    from . import models  # noqa: F401

    runner = "local"
    if use_modal():
        fn = "run_prospect_job" if job.kind == "prospect" else "run_match_job"
        if _spawn_modal(fn, job.id, **kwargs):
            runner = "modal"

    if runner == "local":
        if job.kind == "prospect":
            background_tasks.add_task(
                execute_prospect_job, job.id,
                force_fresh=bool(kwargs.get("force_fresh", False)),
            )
        else:
            background_tasks.add_task(execute_match_job, job.id)

    job.runner = runner
    db.commit()
    return runner
