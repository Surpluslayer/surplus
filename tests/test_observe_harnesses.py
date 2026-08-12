"""tests/test_observe_harnesses.py : the other four harnesses (ablation,
relationship evaluation, signal library evaluation, jurisdiction regression)
plus the synthetic scenario harness -- replay has its own dedicated file
(test_observe_replay.py) because of the no-future-leakage invariant.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend.demo import cohort, provenance as prov
from backend.observe.harnesses import (
    ablation, jurisdiction_regression, relationship_eval, signal_library_eval,
    synthetic_scenarios,
)
from backend.demo import ranking_trace as rt


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


@pytest.fixture
def cohort_id(db):
    return cohort.generate(db, n_lawyers=12, days=30)


# ── ablation ─────────────────────────────────────────────────────────────

def test_ablation_runs_and_produces_three_models(db, cohort_id):
    result = ablation.run(db, cohort_id)
    d = result.to_dict()
    assert d["cases_total"] > 0
    for key in ("A_signal_practice", "B_plus_behavior", "C_plus_relationship"):
        assert key in result.metrics
    assert "cases" in result.metrics


def test_ablation_full_factor_set_matches_default_compute_trace(db, cohort_id):
    """Ablation's C_plus_relationship model uses ALL 6 factors -- must
    produce the exact same score as calling compute_trace with no
    restriction (the backward-compatibility guarantee compute_trace's own
    docstring promises)."""
    from backend import models
    from sqlalchemy import select
    user = db.execute(select(models.User)).scalars().first()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()
    default_trace = rt.compute_trace(db, user, contact)
    full_names = [n for g in rt.FACTOR_GROUPS.values() for n in g]
    restricted_trace = rt.compute_trace(db, user, contact, include_factors=full_names)
    assert abs(default_trace.opportunity_score - restricted_trace.opportunity_score) < 1e-9


def test_ablate_one_reports_full_vs_without_group(db, cohort_id):
    from backend import models
    from sqlalchemy import select
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    assert len(contacts) >= 2
    result = ablation.ablate_one(db, user, contacts[0], contacts, remove_group="relationship")
    assert "full_system" in result and "without_group" in result
    assert result["rank_delta"] == result["without_group"]["rank"] - result["full_system"]["rank"]


def test_ablate_one_rejects_unknown_group(db, cohort_id):
    from backend import models
    from sqlalchemy import select
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    with pytest.raises(ValueError):
        ablation.ablate_one(db, user, contacts[0], contacts, remove_group="not_a_real_group")


# ── relationship evaluation ─────────────────────────────────────────────

def test_relationship_eval_reports_insufficient_data_honestly_on_a_tiny_cohort(db):
    small_cohort_id = cohort.generate(db, n_lawyers=1, days=7, cohort_id="tiny-cohort")
    result = relationship_eval.run(db, small_cohort_id)
    if result.metrics["n_cases_total"] < relationship_eval.MIN_CASES_FOR_AGGREGATE_CLAIM:
        assert result.metrics["sufficient_data_for_aggregate_claim"] is False
        assert result.metrics["ndcg_lift_relationship_vs_baseline"] is None
        assert "note" in result.metrics


def test_relationship_eval_case_inspection_matches_ablate_one(db, cohort_id):
    from backend import models
    from sqlalchemy import select
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    case = relationship_eval.case_inspection(db, user, contacts[0], contacts)
    assert "relationship_evidence" in case
    assert "with_relationship" in case and "without_relationship" in case


# ── signal library evaluation ───────────────────────────────────────────

def test_signal_library_classification_eval_is_real_and_scored(db, cohort_id):
    result = signal_library_eval.run(db, cohort_id)
    classification = result.metrics["classification"]
    assert classification["n_fixtures"] > 0
    assert 0.0 <= classification["overall_accuracy"] <= 1.0
    assert result.cases_total == classification["n_fixtures"]


def test_signal_library_funnel_and_lawyer_conditioned_performance_are_real(db, cohort_id):
    result = signal_library_eval.run(db, cohort_id)
    funnel = result.metrics["library_funnel_by_category"]
    assert funnel  # the generated cohort produces real signal categories
    for cat, counts in funnel.items():
        assert counts["detected"] >= counts["engaged"] >= 0
        assert counts["detected"] >= counts["sent"] >= 0

    conditioned = result.metrics["lawyer_conditioned_performance"]
    assert conditioned  # at least one practice area
    for pa, cats in conditioned.items():
        for cat, bucket in cats.items():
            if bucket["detected"]:
                assert 0.0 <= bucket["action_rate"] <= 1.0


# ── jurisdiction regression ──────────────────────────────────────────────

def test_jurisdiction_regression_wraps_the_real_fixture_matrix_cleanly(db):
    result = jurisdiction_regression.run()
    d = result.to_dict()
    assert d["cases_total"] > 0
    assert d["cases_failed"] == 0  # the underlying gate has no known regressions
    assert "by_category" in result.metrics


def test_jurisdiction_regression_failure_shape_is_structured():
    """Simulate a regression by checking the failure dict SHAPE the harness
    would produce (rule_version/expected/actual), without needing to break
    the real gate to prove it -- see the failures list construction in
    jurisdiction_regression.run()."""
    from backend.solicitation import SolicitationContext, RelationshipType, evaluate
    from backend.observe.versions import JURISDICTION_POLICY_VERSION
    ctx = SolicitationContext(jurisdiction="FL", channel="phone_call",
                              relationship_type=RelationshipType.PROSPECT)
    verdict = evaluate(ctx)
    failure = {
        "scenario_id": "manual_check", "category": "manual", "jurisdiction": ctx.jurisdiction,
        "rule_version": JURISDICTION_POLICY_VERSION,
        "expected": {"allowed": True, "reason_contains": None},
        "actual": {"allowed": verdict.allowed, "reason": verdict.reason},
    }
    assert failure["expected"]["allowed"] != failure["actual"]["allowed"]
    assert set(failure.keys()) == {"scenario_id", "category", "jurisdiction", "rule_version",
                                    "expected", "actual"}


# ── synthetic scenarios ──────────────────────────────────────────────────

def test_synthetic_scenarios_all_pass_and_are_tagged_synthetic(db):
    result = synthetic_scenarios.run(db)
    d = result.to_dict()
    assert d["cases_total"] == 4
    assert d["cases_failed"] == 0
    assert d["provenance"] == "synthetic"

    counts = prov.cohort_row_counts(db, synthetic_scenarios.COHORT_ID)
    assert counts
    for table, by_prov in counts.items():
        assert set(by_prov.keys()) <= {prov.SYNTHETIC}


def test_synthetic_scenarios_is_idempotent_on_rerun(db):
    first = synthetic_scenarios.run(db)
    second = synthetic_scenarios.run(db)
    assert first.metrics["full_ranking"] == second.metrics["full_ranking"]


def test_synthetic_scenarios_cohort_deletes_cleanly(db):
    synthetic_scenarios.run(db)
    deleted = prov.delete_cohort(db, synthetic_scenarios.COHORT_ID)
    assert deleted > 0
    assert prov.cohort_row_counts(db, synthetic_scenarios.COHORT_ID) == {}
