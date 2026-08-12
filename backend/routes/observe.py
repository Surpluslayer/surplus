"""backend/routes/observe.py : Surplus Observe -- /api/observe/*.

Real account auth (backend.auth.current_user), not a shared demo token: this
IS the account-aware, private layer the Observe spec's success criterion #1
requires ("It authenticates against an existing Surplus account"). This is an
ADD-ON surface -- it reads existing production/demo data through the
adapters and harnesses built in backend/observe/, and never writes to a
production object it inspects (the only writes anywhere in this router are
the synthetic-scenario and replay harnesses' own tagged, deletable rows,
identical to backend/demo/cohort.py's existing provenance discipline).

Authorization model: DEMO-provenance users (backend/demo cohorts, the
synthetic-scenario lawyer) are inspectable by ANY signed-in Surplus account --
that shared, seeded dataset is the whole point of a public/investor-facing
observability demo. REAL (non-demo) data stays strictly self-scoped: a
signed-in account may only inspect ITS OWN contacts/interactions, never
another real lawyer's book. `_authorize()` enforces exactly this rule once,
reused by every route below. 404 (not 403) throughout -- same
"don't leak existence" posture already used by routes/demo_observability.py.
"""
from __future__ import annotations

from datetime import datetime

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import models
from ..auth import current_user
from ..db import get_db
from ..observe import adapters, logstream, pipeline
from ..observe import bus as logstream_bus
from ..observe.harnesses import ablation, jurisdiction_regression, relationship_eval
from ..observe.harnesses import replay as replay_harness
from ..observe.harnesses import signal_library_eval, synthetic_scenarios

router = APIRouter(prefix="/api/observe", tags=["observe"])


# ── object browsing (so the Observe panel never has to touch the separate,
#    token-gated /api/demo/observability/* surface -- one auth model here) ──

@router.get("/cohorts")
def list_cohorts(user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    """Lists every provenance-tagged cohort, plus `evaluation_cohort_id` --
    the one the data-driven harnesses should actually evaluate against.
    Callers must use that field rather than picking an arbitrary element of
    `cohorts`: the synthetic known-answer fixture cohort also appears in
    this list, and selecting it silently evaluates the aggregate harnesses
    over 4 hand-built contacts (see logstream.evaluation_cohort_id)."""
    rows = db.execute(
        select(models.DemoProvenance.cohort_id, models.DemoProvenance.data_provenance).distinct()
    ).all()
    cohorts: dict = {}
    for cohort_id, provenance in rows:
        cohorts.setdefault(cohort_id, []).append(provenance)
    return {
        "cohorts": [{"cohort_id": c, "provenance": p} for c, p in cohorts.items()],
        "evaluation_cohort_id": logstream.evaluation_cohort_id(db),
    }


@router.get("/book")
def observe_book(cohort_id: str = Query(...), user_email: str = Query(...),
                  caller: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    """The object list an Observe panel needs to render "click a contact" --
    ONE demo lawyer's signaled contacts, ranked. Real (non-demo) callers may
    only pass their own email (see _authorize)."""
    target = db.execute(select(models.User).where(models.User.email == user_email)).scalar_one_or_none()
    _authorize(caller, target)

    from ..demo import ranking_trace as rt
    contact_tags = db.execute(
        select(models.DemoProvenance).where(models.DemoProvenance.cohort_id == cohort_id,
                                             models.DemoProvenance.table_name == "contacts")
    ).scalars().all()
    contacts = db.execute(
        select(models.Contact).where(models.Contact.id.in_([t.row_id for t in contact_tags]),
                                      models.Contact.user_id == target.id)
    ).scalars().all()
    signaled = [c for c in contacts if rt._latest_signal(db, c.id) is not None]
    traces = rt.rank_opportunities(db, target, signaled)

    def _latest_draft_id(contact_id: int):
        row = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id == contact_id,
                   models.RelationshipInteraction.title.like("Drafted follow-up%"))
            .order_by(models.RelationshipInteraction.occurred_at.desc()).limit(1)
        ).scalar_one_or_none()
        return row.id if row else None

    return {
        "user": {"email": target.email, "id": target.id, "practice_area": target.practice_area,
                 "bar_jurisdiction": target.bar_jurisdiction},
        "opportunities": [{"contact_id": t.contact_id, "contact_name": t.contact_name,
                           "rank": t.rank, "score": round(t.opportunity_score, 4),
                           "latest_draft_interaction_id": _latest_draft_id(t.contact_id)}
                          for t in traces],
    }


# ── shared authorization + lookup helpers ───────────────────────────────────

def _authorize(caller: models.User, target) -> models.User:
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    if target.is_demo or target.id == caller.id:
        return target
    raise HTTPException(status_code=404, detail="not found")


def _owned_contact(db: DbSession, caller: models.User, contact_id: int):
    contact = db.get(models.Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="not found")
    owner = db.get(models.User, contact.user_id)
    _authorize(caller, owner)
    return owner, contact


def _owned_interaction(db: DbSession, caller: models.User, interaction_id: int):
    row = db.get(models.RelationshipInteraction, interaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    owner = db.get(models.User, row.actor_user_id)
    _authorize(caller, owner)
    return row


def _candidate_contacts(db: DbSession, owner: models.User, candidate_ids: str | None) -> list:
    if candidate_ids:
        ids = [int(x) for x in candidate_ids.split(",") if x.strip()]
        return list(db.execute(
            select(models.Contact).where(models.Contact.id.in_(ids),
                                          models.Contact.user_id == owner.id)
        ).scalars().all())
    return list(db.execute(
        select(models.Contact).where(models.Contact.user_id == owner.id)
    ).scalars().all())


# ── streaming execution log (SSE) ──────────────────────────────────────────
# Same shape as routes/book.py's own SSE endpoints: a sync generator with its
# OWN SessionLocal (the request-scoped session closes before the body is
# consumed), an immediate ": open" flush, and X-Accel-Buffering: no so bytes
# reach the browser continuously instead of buffering at the edge until the
# 524 gateway timeout fires -- which is exactly what made the old
# fetch-and-wait Pipeline card time out and render a Cloudflare error page.

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# How long one /stream/activity connection lives before closing so the client
# reconnects. Sync SSE handlers occupy a threadpool worker and only notice a
# vanished client when a write fails, so an unbounded loop leaks a worker per
# abandoned tab. EventSource reconnects automatically, so the reader sees a
# continuous tail either way. Comfortably under Cloudflare's 100s idle read
# timeout is unnecessary (the heartbeat handles that); this is about
# reclaiming threads.
_ACTIVITY_STREAM_MAX_S = 600.0


def _sse(gen_events):
    def gen():
        yield ": open\n\n"
        from ..db import SessionLocal
        wdb = SessionLocal()
        try:
            for ev in gen_events(wdb):
                yield f"event: log\ndata: {json.dumps(ev)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as exc:  # noqa: BLE001
            payload = {"detail": f"{type(exc).__name__}: {exc}"}
            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
        finally:
            wdb.close()
    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/stream/boot")
def stream_boot(user: models.User = Depends(current_user)):
    """Report the live updates pipeline, then run every harness for real,
    streaming one line per execution."""
    user_id = user.id

    def events(wdb):
        # Re-load in the stream's own session; the request-scoped one is
        # closed by the time the body is consumed.
        return logstream.boot_events(wdb, wdb.get(models.User, user_id))

    return _sse(events)


@router.get("/stream/activity")
def stream_activity(
    user: models.User = Depends(current_user),
    db: DbSession = Depends(get_db),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Tail what the PRODUCT is doing right now -- ask-bar runs and Draft
    taps, narrated with the real machinery each one sets off (which model,
    cache, gate, fallback). Fed by backend/observe/bus.py, which
    routes/book.py publishes to as those requests run.

    Long-lived: holds the connection open and polls, emitting a comment
    heartbeat so the edge can't time it out.

    Two things this endpoint must NOT do, both learned the hard way:

    1. Hold a pooled DB connection. `Depends(current_user)` resolves through
       get_db, and FastAPI keeps a dependency's resources alive until the
       RESPONSE COMPLETES -- which for an endless generator is never. Every
       open Observe tab would permanently consume one of DB_POOL_SIZE +
       DB_MAX_OVERFLOW (8 by default) connections until the pool was gone and
       the whole app started timing out on QueuePool. So the user id is read
       out and the request-scoped session is CLOSED here, before a single byte
       is streamed, returning its connection to the pool immediately. FastAPI
       closes it again when the response ends; Session.close() is idempotent.
       Each poll then opens and closes its own short session via bus.py.

    2. Run forever. An SSE connection the client abandoned still occupies a
       threadpool worker, and sync handlers only notice the disconnect when
       they try to write. The loop is bounded; EventSource reconnects on its
       own, so a bounded stream is invisible to the reader but frees the
       thread on a schedule.
    """
    import time as _time

    # Read the id BEFORE closing: close() expunges the instance, and a later
    # attribute load on a detached object would raise.
    account_id = user.id
    db.close()   # see 1 above -- hand the pooled connection back now

    # Resume where the last connection stopped. EventSource replays the id of
    # the last event it saw in Last-Event-ID automatically on reconnect, so a
    # bounded stream (above) hands off without dropping lines. Absent that
    # header this is a fresh reader: start at the current tip so it sees new
    # activity rather than replaying the retention window.
    try:
        resume_from = int(last_event_id) if last_event_id else None
    except (TypeError, ValueError):
        resume_from = None

    def gen():
        yield ": open\n\n"
        cursor = resume_from if resume_from is not None else logstream_bus.latest_seq()
        started = _time.monotonic()
        last_beat = started
        while _time.monotonic() - started < _ACTIVITY_STREAM_MAX_S:
            events = logstream_bus.since(account_id, cursor)
            for ev in events:
                cursor = max(cursor, ev["seq"])
                yield f"id: {ev['seq']}\nevent: log\ndata: {json.dumps(ev)}\n\n"
            if events:
                last_beat = _time.monotonic()
            elif _time.monotonic() - last_beat > 15:
                last_beat = _time.monotonic()
                yield ": keepalive\n\n"
            _time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/stream/contact/{contact_id}")
def stream_contact(contact_id: int, channel: str = Query(default="linkedin_dm"),
                    user: models.User = Depends(current_user),
                    db: DbSession = Depends(get_db)):
    """Stream the real per-contact computation: pipeline stages, the ranking
    arithmetic, ablation deltas, and the jurisdiction rule set applied line
    by line to the real drafted message."""
    owner, contact = _owned_contact(db, user, contact_id)
    owner_id, contact_id_ = owner.id, contact.id

    def events(wdb):
        w_owner = wdb.get(models.User, owner_id)
        w_contact = wdb.get(models.Contact, contact_id_)
        if w_owner is None or w_contact is None:
            yield logstream.line(logstream.ERR, "backend.routes.observe:stream_contact",
                                  "contact vanished between request and stream")
            return
        for ev in logstream.contact_events(wdb, w_owner, w_contact, channel):
            yield ev

    return _sse(events)


# ── universal "why?" trace endpoints (one per object type) ─────────────────

@router.get("/trace/signal/{interaction_id}")
def trace_signal(interaction_id: int, user: models.User = Depends(current_user),
                  db: DbSession = Depends(get_db)):
    row = _owned_interaction(db, user, interaction_id)
    return adapters.signal_trace(db, row).to_dict()


@router.get("/trace/draft/{interaction_id}")
def trace_draft(interaction_id: int, user: models.User = Depends(current_user),
                 db: DbSession = Depends(get_db)):
    row = _owned_interaction(db, user, interaction_id)
    return adapters.draft_trace(db, row).to_dict()


@router.get("/trace/outcome/{interaction_id}")
def trace_outcome(interaction_id: int, user: models.User = Depends(current_user),
                   db: DbSession = Depends(get_db)):
    row = _owned_interaction(db, user, interaction_id)
    return adapters.outcome_trace(db, row).to_dict()


@router.get("/trace/lawyer/{contact_id}")
def trace_lawyer(contact_id: int, user: models.User = Depends(current_user),
                  db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    return adapters.targeting_trace(db, owner, contact).to_dict()


@router.get("/trace/relationship/{contact_id}")
def trace_relationship(contact_id: int, user: models.User = Depends(current_user),
                        db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    return adapters.relationship_trace(db, owner, contact).to_dict()


@router.get("/trace/opportunity/{contact_id}")
def trace_opportunity(contact_id: int, candidate_ids: str | None = Query(default=None),
                       user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    candidates = _candidate_contacts(db, owner, candidate_ids)
    if contact not in candidates:
        candidates.append(contact)
    return adapters.opportunity_trace(db, owner, contact, candidates).to_dict()


@router.get("/trace/jurisdiction/{contact_id}")
def trace_jurisdiction(contact_id: int, channel: str = Query(default="linkedin_dm"),
                        user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    return adapters.jurisdiction_trace(db, owner, contact, channel).to_dict()


@router.get("/trace/pipeline/{contact_id}")
def trace_pipeline(contact_id: int, candidate_ids: str | None = Query(default=None),
                    channel: str = Query(default="linkedin_dm"),
                    user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    candidates = _candidate_contacts(db, owner, candidate_ids)
    return pipeline.compute_pipeline_trace(db, owner, contact, candidates, channel).to_dict()


@router.get("/trace/signal_library/{category}")
def trace_signal_library(category: str, cohort_id: str = Query(...),
                          user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    return adapters.signal_library_trace(db, user.id, cohort_id, category).to_dict()


# ── harness run endpoints ───────────────────────────────────────────────────

@router.get("/harness/ablation")
def harness_ablation(cohort_id: str = Query(...), k: int = Query(default=5),
                      user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    return ablation.run(db, cohort_id, k=k).to_dict()


@router.get("/harness/relationship_evaluation")
def harness_relationship_evaluation(cohort_id: str = Query(...), k: int = Query(default=5),
                                     user: models.User = Depends(current_user),
                                     db: DbSession = Depends(get_db)):
    return relationship_eval.run(db, cohort_id, k=k).to_dict()


@router.get("/harness/signal_library_evaluation")
def harness_signal_library_evaluation(cohort_id: str = Query(...),
                                       user: models.User = Depends(current_user),
                                       db: DbSession = Depends(get_db)):
    return signal_library_eval.run(db, cohort_id).to_dict()


@router.get("/harness/jurisdiction_regression")
def harness_jurisdiction_regression(user: models.User = Depends(current_user)):
    return jurisdiction_regression.run().to_dict()


@router.get("/harness/historical_replay")
def harness_historical_replay(cohort_id: str = Query(...),
                               user: models.User = Depends(current_user),
                               db: DbSession = Depends(get_db)):
    return replay_harness.run(db, cohort_id).to_dict()


@router.get("/harness/synthetic_scenarios")
def harness_synthetic_scenarios(user: models.User = Depends(current_user),
                                 db: DbSession = Depends(get_db)):
    return synthetic_scenarios.run(db).to_dict()


# ── individual-case drill-downs (the strongest UI demos) ───────────────────

@router.get("/ablate/{contact_id}")
def ablate_one(contact_id: int, remove_group: str = Query(default="relationship"),
                candidate_ids: str | None = Query(default=None),
                user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    candidates = _candidate_contacts(db, owner, candidate_ids)
    if contact not in candidates:
        candidates.append(contact)
    try:
        return ablation.ablate_one(db, owner, contact, candidates, remove_group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/relationship_evaluation/case/{contact_id}")
def relationship_eval_case(contact_id: int, candidate_ids: str | None = Query(default=None),
                            user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    candidates = _candidate_contacts(db, owner, candidate_ids)
    if contact not in candidates:
        candidates.append(contact)
    return relationship_eval.case_inspection(db, owner, contact, candidates)


@router.get("/replay/case/{contact_id}")
def replay_case(contact_id: int, as_of: datetime = Query(...),
                 user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    owner, contact = _owned_contact(db, user, contact_id)
    return replay_harness.replay_one(db, owner, contact, as_of)
