"""backend/observe/harnesses/ablation.py : HARNESS #2 -- feature ablation.

Answers "which pieces of Surplus intelligence actually change the
recommendation?" by running the SAME candidate set through progressively
richer factor groups (ranking_trace.FACTOR_GROUPS) and comparing rank/score --
the candidate set never changes between runs, only which features contribute
to the score (ranking_trace.compute_trace's include_factors param,
renormalized so a smaller factor set still produces a comparable 0-1 score;
see that function's own docstring).

Two entry points:
  - run(db, cohort_id): aggregate ablation across every lawyer in a cohort,
    with Precision/Recall/MRR/NDCG@K per model plus rank_delta/score_delta
    for individual cases.
  - ablate_one(db, user, contact, candidates, remove_group): the single-
    opportunity "Ablate relationship" UI action -- full system vs system-
    without-one-group, for exactly the object the user clicked.
"""
from __future__ import annotations

from .. import cohort_query as cq
from .. import metrics as m
from ..harness import HarnessResult, now as harness_now
from ...demo import ranking_trace as rt

HARNESS_ID = "ablation"
HARNESS_NAME = "Feature Ablation Harness"
HARNESS_VERSION = "v1"

# Progressive-richness models, each additive over the previous -- Observe
# spec section 5's A/B/C. Reuses ranking_trace.FACTOR_GROUPS directly so this
# harness can never drift from the factor definitions it's ablating.
MODEL_GROUPS = {
    "A_signal_practice": ("signal_practice",),
    "B_plus_behavior": ("signal_practice", "behavior"),
    "C_plus_relationship": ("signal_practice", "behavior", "relationship"),
}

# Payload-size cap on the individual-case list returned in a harness run's
# metrics -- a large cohort can produce thousands of cases; the Observe panel
# only ever needs a representative sample plus the aggregate metrics.
MAX_CASES_IN_RESULT = 50


def _factor_names(model_key: str) -> list:
    names = []
    for group in MODEL_GROUPS[model_key]:
        names.extend(rt.FACTOR_GROUPS[group])
    return names


def _rank_with_model(db, user, contacts, model_key: str) -> dict:
    traces = rt.rank_opportunities(db, user, contacts, include_factors=_factor_names(model_key))
    return {t.contact_id: (t.rank, t.opportunity_score) for t in traces}


def ablate_one(db, user, contact, candidates: list, remove_group: str,
                data=None) -> dict:
    """Single-opportunity UI action: full-system score/rank for `contact`
    among `candidates`, vs. the same ranking with one factor group removed.
    `remove_group` must be a key in ranking_trace.FACTOR_GROUPS -- including
    the two overlapping named levers ("signal_affinity", "timing") alongside
    the canonical partition ("signal_practice", "behavior", "relationship").

    Deliberately NOT built by summing FACTOR_GROUPS' entries together: two of
    its groups overlap (signal_affinity subsets signal_practice; timing
    subsets relationship -- see that dict's own docstring), so summing would
    double-count a factor and silently over-weight it. Instead: the full
    system is always every factor in FACTOR_WEIGHTS, and "without" is that
    complete set minus exactly the removed group's factors -- correct
    regardless of overlap."""
    if remove_group not in rt.FACTOR_GROUPS:
        raise ValueError(f"unknown factor group: {remove_group!r}")
    full_names = list(rt.FACTOR_WEIGHTS.keys())
    removed = set(rt.FACTOR_GROUPS[remove_group])
    without_names = [n for n in full_names if n not in removed]

    # Both rankings score the SAME candidates from the SAME rows -- only the
    # factor subset differs -- so the underlying data is loaded once and
    # shared. Callers looping over several levers should pass `data` in so it
    # is loaded once for the whole loop rather than once per lever.
    if data is None:
        data = rt.prefetch(db, user, candidates)
    full = rt.rank_opportunities(db, user, candidates, include_factors=full_names,
                                 data=data)
    without = rt.rank_opportunities(db, user, candidates, include_factors=without_names,
                                    data=data)
    full_by_id = {t.contact_id: t for t in full}
    without_by_id = {t.contact_id: t for t in without}
    ft, wt = full_by_id[contact.id], without_by_id[contact.id]

    return {
        "contact_id": contact.id,
        "removed_group": remove_group,
        "full_system": {"rank": ft.rank, "score": round(ft.opportunity_score, 4)},
        "without_group": {"rank": wt.rank, "score": round(wt.opportunity_score, 4)},
        "rank_delta": wt.rank - ft.rank,
        "score_delta": round(ft.opportunity_score - wt.opportunity_score, 4),
    }


def run(db, cohort_id: str, k: int = 5) -> HarnessResult:
    pairs = cq.users_and_contacts(db, cohort_id)

    per_model_relevance_lists = {key: [] for key in MODEL_GROUPS}
    cases = []
    lawyers_considered = 0
    lawyers_with_evaluable_outcome = 0

    for user, contacts in pairs:
        lawyers_considered += 1
        relevance_by_id = {c.id: cq.most_recent_draft_relevance(db, c.id) for c in contacts}
        if not any(v is not None for v in relevance_by_id.values()):
            continue
        lawyers_with_evaluable_outcome += 1

        model_ranks = {key: _rank_with_model(db, user, contacts, key) for key in MODEL_GROUPS}

        for key in MODEL_GROUPS:
            ranked_ids = sorted(model_ranks[key], key=lambda cid: model_ranks[key][cid][0])
            relevances = [relevance_by_id.get(cid) or 0.0 for cid in ranked_ids]
            per_model_relevance_lists[key].append(relevances)

        for c in contacts:
            rel = relevance_by_id[c.id]
            if rel is None:
                continue
            a_rank, a_score = model_ranks["A_signal_practice"][c.id]
            c_rank, c_score = model_ranks["C_plus_relationship"][c.id]
            cases.append({
                "user_email": user.email, "contact_id": c.id, "contact_name": c.name,
                "relevance": rel,
                "model_A": {"rank": a_rank, "score": round(a_score, 4)},
                "model_C": {"rank": c_rank, "score": round(c_score, 4)},
                "rank_delta": a_rank - c_rank,
                "score_delta": round(c_score - a_score, 4),
            })

    metrics = {}
    for key, per_lawyer_relevances in per_model_relevance_lists.items():
        metrics[key] = {
            "precision_at_k": m.mean_or_none([m.precision_at_k(r, k) for r in per_lawyer_relevances]),
            "recall_at_k": m.mean_or_none([m.recall_at_k(r, k) for r in per_lawyer_relevances]),
            "mrr": m.mean_or_none([m.reciprocal_rank(r) for r in per_lawyer_relevances]),
            "ndcg_at_k": m.mean_or_none([m.ndcg_at_k(r, k) for r in per_lawyer_relevances]),
            "n_lawyers": len(per_lawyer_relevances),
        }
    metrics["mean_rank_delta_A_to_C"] = m.mean_or_none([c["rank_delta"] for c in cases])
    metrics["mean_score_delta_A_to_C"] = m.mean_or_none([c["score_delta"] for c in cases])
    metrics["n_cases"] = len(cases)
    metrics["cases"] = cases[:MAX_CASES_IN_RESULT]

    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=lawyers_considered,
        cases_passed=lawyers_with_evaluable_outcome,
        cases_failed=lawyers_considered - lawyers_with_evaluable_outcome,
        metrics=metrics, failures=[], provenance="demo",
    )
