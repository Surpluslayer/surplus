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
