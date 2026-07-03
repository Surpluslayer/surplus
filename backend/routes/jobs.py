"""routes/jobs.py : async job start + poll for the heavy outbound stages.

Prospecting (search) and matching used to run *synchronously* inside their
POST handlers — the request blocked until run_prospect / compute_match finished
and returned the PipelineResult / MatchResult inline. That's fine on a fast box
but ties up a worker for the whole job and can't be offloaded to Modal.

This router makes them request/response *async*:

    POST /events/{id}/prospect/async  -> { job_id, ... }   (returns immediately)
    POST /events/{id}/match/async     -> { job_id, ... }
    GET  /events/{id}/jobs/{job_id}    -> { status, result?, error? }   (poll)

The work itself is dispatched by backend/jobs.py::dispatch_job — to Modal when
USE_MODAL is on (and reachable), else a local FastAPI BackgroundTask. Either
way the worker writes the serialized PipelineResult / MatchResult onto the Job
row, and the frontend polls the GET endpoint until status == "done".

Auth: every route resolves the event through get_owned_event, so a user can
only start / poll jobs for their own events. The poll endpoint additionally
checks job.event_id == ev.id so a job id can't be used cross-event.
"""
from __future__ import annotations
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user, get_owned_event
from ..db import get_db
from .. import jobs as jobs_mod

router = APIRouter(prefix="/events", tags=["jobs"])


def _job_view(job: models.Job) -> dict:
    """Serialize a Job row for the frontend poller."""
    out = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "runner": job.runner,
    }
    if job.status == "done" and job.result_json:
        try:
            out["result"] = json.loads(job.result_json)
        except (ValueError, TypeError):
            # A truncated / corrupt result_json (container killed mid-write)
            # must not 500 the poller. Surface the raw payload so the client
            # can still finish, rather than raising.
            out["result_raw"] = job.result_json
    if job.status == "error":
        out["error"] = job.error
    return out


@router.post("/{event_id}/prospect/async")
async def start_prospect_job(
    event_id: int,
    background_tasks: BackgroundTasks,
    fresh: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Queue a prospecting (search) job and return its id immediately.

    Mirrors POST /{event_id}/prospect but non-blocking: the PipelineResult
    lands on the Job row, polled via GET /{event_id}/jobs/{job_id}.
    """
    ev = get_owned_event(event_id, user, db)
    job = jobs_mod.new_job(db, event_id=ev.id, user_id=user.id, kind="prospect")
    jobs_mod.dispatch_job(background_tasks, db, job, force_fresh=fresh)
    return _job_view(job)


@router.post("/{event_id}/match/async")
async def start_match_job(
    event_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Queue a matching job and return its id immediately.

    Mirrors POST /{event_id}/match but non-blocking: the MatchResult lands
    on the Job row, polled via GET /{event_id}/jobs/{job_id}.
    """
    ev = get_owned_event(event_id, user, db)
    job = jobs_mod.new_job(db, event_id=ev.id, user_id=user.id, kind="match")
    jobs_mod.dispatch_job(background_tasks, db, job)
    return _job_view(job)


@router.get("/{event_id}/jobs/{job_id}")
async def get_job(
    event_id: int,
    job_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Poll a job's status. Returns the serialized result once status==done."""
    ev = get_owned_event(event_id, user, db)  # authorizes the event
    job = db.get(models.Job, job_id)
    if job is None or job.event_id != ev.id:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_view(job)
