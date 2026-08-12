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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import models
from ..auth import current_user
from ..db import get_db
from ..observe import adapters
from ..observe.harnesses import ablation, jurisdiction_regression, relationship_eval
from ..observe.harnesses import replay as replay_harness
from ..observe.harnesses import signal_library_eval, synthetic_scenarios

router = APIRouter(prefix="/api/observe", tags=["observe"])


# ── object browsing (so the Observe panel never has to touch the separate,
#    token-gated /api/demo/observability/* surface -- one auth model here) ──

@router.get("/cohorts")
def list_cohorts(user: models.User = Depends(current_user), db: DbSession = Depends(get_db)):
    rows = db.execute(
        select(models.DemoProvenance.cohort_id, models.DemoProvenance.data_provenance).distinct()
    ).all()
    cohorts: dict = {}
    for cohort_id, provenance in rows:
        cohorts.setdefault(cohort_id, []).append(provenance)
    return {"cohorts": [{"cohort_id": c, "provenance": p} for c, p in cohorts.items()]}


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
