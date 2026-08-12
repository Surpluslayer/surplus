"""backend/observe/metrics.py : ranking-quality metrics shared by the
ablation and relationship-evaluation harnesses.

Binary relevance (Precision@K/Recall@K/MRR) treats any resolved outcome as
relevant. Graded relevance (NDCG) uses the disposition hierarchy below --
discarded=0 < approved_as_is=1 < edited_then_sent=2 -- the finest distinction
backend/demo/cohort.py's generated data actually supports today. This is NOT
the full ignored/viewed/saved/drafted/edited/sent/replied/meeting hierarchy
the Observe spec describes (section 6) -- that funnel isn't tracked yet.
Grading against three levels instead of eight is the honest ceiling of what's
measurable right now, not a simplification of choice.
"""
from __future__ import annotations

import math

DISPOSITION_GRADE = {"discarded": 0, "approved_as_is": 1, "edited_then_sent": 2}


def precision_at_k(ranked_relevances: list, k: int):
    top = ranked_relevances[:k]
    if not top:
        return None
    return sum(1 for r in top if r and r > 0) / len(top)


def recall_at_k(ranked_relevances: list, k: int):
    total_relevant = sum(1 for r in ranked_relevances if r and r > 0)
    if total_relevant == 0:
        return None
    top = ranked_relevances[:k]
    return sum(1 for r in top if r and r > 0) / total_relevant


def reciprocal_rank(ranked_relevances: list) -> float:
    for i, r in enumerate(ranked_relevances, start=1):
        if r and r > 0:
            return 1.0 / i
    return 0.0


def dcg_at_k(ranked_relevances: list, k: int) -> float:
    return sum((r or 0.0) / math.log2(i + 1)
                for i, r in enumerate(ranked_relevances[:k], start=1))


def ndcg_at_k(ranked_relevances: list, k: int):
    ideal = sorted((r or 0.0 for r in ranked_relevances), reverse=True)
    idcg = dcg_at_k(ideal, k)
    if idcg == 0:
        return None
    return dcg_at_k(ranked_relevances, k) / idcg


def mean_or_none(values: list):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None
