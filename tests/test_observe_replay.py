"""tests/test_observe_replay.py : the explicit no-future-leakage test the
Observe spec's replay harness section requires ("Create an explicit test for
this"). Proves, directly against ranking_trace.compute_trace's as_of param
(not just the replay harness's own aggregate spot-check), that an interaction
occurring AFTER the replay cutoff has ZERO effect on the reconstructed trace
-- including historical_behavior, the factor that leaked before as_of support
existed (see backend/demo/ranking_trace.py's _historical_behavior docstring).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import ranking_trace as rt
from backend.observe.harnesses import replay

_UTC = timezone.utc


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _make_user_and_contact(db, practice_area="corporate_ma"):
    user = models.User(email="replay-test@example.com", name="Replay Test",
                        is_demo=True, practice_area=practice_area)
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="replay:1", name="Target Contact",
                              watched_at=datetime.now(_UTC))
    db.add(contact)
    db.flush()
    db.commit()
    return user, contact


def test_historical_behavior_excludes_interactions_after_as_of(db):
    """THE required invariant test: compute_trace(..., as_of=T) must produce
    an IDENTICAL trace whether or not a later interaction exists in the DB."""
    user, contact = _make_user_and_contact(db)
    now = datetime.now(_UTC)
    t_cutoff = now - timedelta(days=5)

    # Pre-cutoff signal + a resolved prior draft on the SAME category, so
    # historical_behavior has real data to compute from.
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="activity_update",
        interaction_type="message", direction="outbound",
        occurred_at=t_cutoff - timedelta(days=10),
        title="Drafted follow-up (prior)",
        meta_json=json.dumps({"signal_category": "acquisition_announced", "engaged": True,
                              "disposition": "approved_as_is"}),
    ))
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="activity_update",
        interaction_type="message", direction="outbound", occurred_at=t_cutoff - timedelta(days=1),
        title="Drafted follow-up (the replayed signal)",
        meta_json=json.dumps({"signal_category": "acquisition_announced"}),
    ))
    db.commit()

    trace_before = rt.compute_trace(db, user, contact, as_of=t_cutoff)

    # Now add a FUTURE interaction (after t_cutoff) that, if leaked, would
    # change relationship_strength/recency/trajectory AND historical_behavior
    # (a differently-dispositioned same-category draft, which would shift
    # historical_behavior's saved/seen ratio if counted).
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="activity_update",
        interaction_type="message", direction="outbound", occurred_at=now,
        title="Drafted follow-up (future -- must not leak)",
        meta_json=json.dumps({"signal_category": "acquisition_announced", "engaged": False,
                              "disposition": "discarded"}),
    ))
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="note",
        interaction_type="note", direction="none", occurred_at=now,
        title="Reviewed contact",
    ))
    db.commit()

    trace_after = rt.compute_trace(db, user, contact, as_of=t_cutoff)

    assert trace_before.opportunity_score == trace_after.opportunity_score
    before_by_name = {f.name: f.value for f in trace_before.factors}
    after_by_name = {f.name: f.value for f in trace_after.factors}
    assert before_by_name == after_by_name, (
        "future interaction leaked into a point-in-time replay's reconstructed trace"
    )
    before_evidence = {f.name: f.evidence for f in trace_before.factors}
    after_evidence = {f.name: f.evidence for f in trace_after.factors}
    assert before_evidence == after_evidence


def test_replay_one_never_reports_a_future_interaction_count(db):
    """Same invariant, exercised through the harness's own replay_one(), not
    just compute_trace directly -- proves the harness-level wrapper doesn't
    reintroduce leakage on top of an already-safe compute_trace."""
    user, contact = _make_user_and_contact(db)
    now = datetime.now(_UTC)
    as_of = now - timedelta(days=3)

    for i in range(3):
        db.add(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id, source_type="note",
            interaction_type="note", direction="none",
            occurred_at=as_of - timedelta(days=i + 1), title="Reviewed contact",
        ))
    db.commit()
    true_pre_cutoff_count = 3

    for i in range(5):
        db.add(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id, source_type="note",
            interaction_type="note", direction="none",
            occurred_at=now - timedelta(hours=i), title="Reviewed contact",
        ))
    db.commit()

    result = replay.replay_one(db, user, contact, as_of)
    strength = next(f for f in result["reconstructed_factors"] if f["name"] == "relationship_strength")
    assert strength["evidence"]["total_interactions"] == true_pre_cutoff_count


def test_run_reports_zero_leakage_violations_on_a_real_cohort(db):
    from backend.demo import cohort
    cohort_id = cohort.generate(db, n_lawyers=6, days=30)
    result = replay.run(db, cohort_id)
    assert result.metrics["temporal_leakage_violations"] == 0
    assert result.cases_passed > 0
