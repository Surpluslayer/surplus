"""backend/observe/harnesses/relationship_eval.py : HARNESS #3 -- the
centerpiece.

Research question: given a lawyer and a potential BD event, does knowledge of
their professional relationship network improve prediction of which
opportunity they act on -- beyond signal + practice + behavior alone? NOT
"does this relationship score look reasonable" -- that isn't a testable
claim (Observe spec section 6). This harness reuses ablation.py's exact
Model A/B/C machinery and cohort data; what it adds is the "insufficient
data" honesty path ablation.py doesn't need -- ablation is a technical demo
of mechanism (does removing a factor group change the score, at all,
mechanically), while this harness is a claim about predictive value, so it
has to say when N is too small to claim anything (section 15: "do not
manufacture significance").
"""
from __future__ import annotations

import json

from sqlalchemy import select

from ..harness import HarnessResult, now as harness_now
from . import ablation

HARNESS_ID = "relationship_evaluation"
HARNESS_NAME = "Relationship Evaluation Harness"
HARNESS_VERSION = "v1"

# Below this many resolved-outcome cases, report relationship-sensitive-case
# counts instead of an aggregate NDCG lift. Not a statistically rigorous
# threshold -- a conservative floor chosen so single-digit sample sizes never
# get reported as a percentage lift (Observe spec section 6's own example:
# "Relationship-sensitive cases: 17" instead of a lift number).
MIN_CASES_FOR_AGGREGATE_CLAIM = 20


def run(db, cohort_id: str, k: int = 5) -> HarnessResult:
    ablation_result = ablation.run(db, cohort_id, k=k)
    cases = ablation_result.metrics.get("cases", [])
    n_cases = ablation_result.metrics.get("n_cases", len(cases))

    relationship_sensitive = [c for c in cases if c["rank_delta"] != 0]
    aligned_with_outcome = [
        c for c in relationship_sensitive
        if c["relevance"] > 0 and c["model_C"]["rank"] < c["model_A"]["rank"]
    ]

    metrics = {
        "n_cases_sampled": len(cases),
        "n_cases_total": n_cases,
        "relationship_sensitive_cases": len(relationship_sensitive),
        "cases_where_change_aligned_with_observed_action": len(aligned_with_outcome),
        "model_A_signal_practice": ablation_result.metrics.get("A_signal_practice"),
        "model_B_plus_behavior": ablation_result.metrics.get("B_plus_behavior"),
        "model_C_plus_relationship": ablation_result.metrics.get("C_plus_relationship"),
    }

    if n_cases >= MIN_CASES_FOR_AGGREGATE_CLAIM:
        ndcg_a = (metrics["model_A_signal_practice"] or {}).get("ndcg_at_k")
        ndcg_c = (metrics["model_C_plus_relationship"] or {}).get("ndcg_at_k")
        metrics["ndcg_lift_relationship_vs_baseline"] = (
            round(ndcg_c - ndcg_a, 4) if ndcg_a is not None and ndcg_c is not None else None
        )
        metrics["sufficient_data_for_aggregate_claim"] = True
    else:
        metrics["ndcg_lift_relationship_vs_baseline"] = None
        metrics["sufficient_data_for_aggregate_claim"] = False
        metrics["note"] = (
            f"Only {n_cases} resolved-outcome cases (< {MIN_CASES_FOR_AGGREGATE_CLAIM}) -- "
            "reporting relationship-sensitive case counts instead of an aggregate NDCG "
            "lift. Not enough data to claim statistical significance."
        )

    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=ablation_result.cases_total,
        cases_passed=ablation_result.cases_passed,
        cases_failed=ablation_result.cases_failed,
        metrics=metrics, failures=[], provenance="demo",
    )


def case_inspection(db, user, contact, candidates: list) -> dict:
    """Individual-case drill-down for the Observe panel: relationship
    evidence for this contact, full-system vs without-relationship rank/
    score, and the observed outcome -- Observe spec section 6's "strongest
    technical demo in the entire system"."""
    from ... import models
    from ...demo import ranking_trace as rt

    trace = rt.compute_trace(db, user, contact)
    rel_factor_names = {"relationship_strength", "relationship_recency", "relationship_trajectory"}
    rel_factors = {f.name: f for f in trace.factors if f.name in rel_factor_names}

    ablated = ablation.ablate_one(db, user, contact, candidates, remove_group="relationship")

    outcome_row = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact.id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
        .order_by(models.RelationshipInteraction.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    outcome = None
    if outcome_row is not None:
        meta = json.loads(outcome_row.meta_json or "{}")
        outcome = {"engaged": meta.get("engaged"), "disposition": meta.get("disposition")}

    return {
        "contact_id": contact.id, "contact_name": contact.name,
        "relationship_evidence": {name: f.evidence for name, f in rel_factors.items()},
        "with_relationship": ablated["full_system"],
        "without_relationship": ablated["without_group"],
        "rank_delta": ablated["rank_delta"],
        "score_delta": ablated["score_delta"],
        "observed_outcome": outcome,
    }
