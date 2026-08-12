"""backend/demo/eval_backtest.py : ranker v0 vs v1, top-5 historical outcome
rate -- computed for real against the generated cohort's own outcome data,
never a fabricated pair of numbers.

"Only if you actually have enough data to calculate it": this cohort's
draft-disposition data (backend/demo/cohort.py, meta_json.engaged) is
exactly that data, so the comparison below is real arithmetic over real
rows, honestly scoped as demo-cohort-only -- not a stand-in for a live
customer backtest, and not presented as one.

v0 (naive): ranks contacts by relationship_recency alone -- the kind of
ranker you'd ship in a weekend, no signal/practice/history awareness.
v1 (full): backend/demo/ranking_trace.py's full multi-factor score.

Top-5 outcome rate: of a lawyer's top-5 ranked contacts (by whichever
ranker), what fraction had their most recent drafted signal actually
engaged with (approved_as_is / edited_then_sent, vs discarded)? Averaged
across every lawyer in the cohort who has at least one drafted-and-resolved
signal to evaluate against.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from . import ranking_trace as rt
from .. import models


@dataclass
class BacktestResult:
    n_lawyers_evaluated: int
    v0_top5_outcome_rate: float
    v1_top5_outcome_rate: float
    v0_n_opportunities: int
    v1_n_opportunities: int

    def to_dict(self) -> dict:
        return {
            "n_lawyers_evaluated": self.n_lawyers_evaluated,
            "v0_naive_recency_ranker": {"top5_positive_outcome_rate": round(self.v0_top5_outcome_rate, 4),
                                          "opportunities_evaluated": self.v0_n_opportunities},
            "v1_full_ranking_trace": {"top5_positive_outcome_rate": round(self.v1_top5_outcome_rate, 4),
                                        "opportunities_evaluated": self.v1_n_opportunities},
            "lift": round(self.v1_top5_outcome_rate - self.v0_top5_outcome_rate, 4),
        }


def _most_recent_outcome(db, contact_id: int) -> bool | None:
    """Was this contact's most recent drafted signal engaged with? None if
    the contact never had a draft to resolve (excluded from the backtest,
    not counted as a negative)."""
    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact_id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
        .order_by(models.RelationshipInteraction.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if rows is None:
        return None
    meta = json.loads(rows.meta_json or "{}")
    return bool(meta.get("engaged"))


def _v0_naive_rank(db, contacts: list) -> list:
    """Recency-only ranking -- no signal, practice, or history awareness."""
    scored = [(c, rt._relationship_recency(rt._all_interactions(db, c.id)).value)
              for c in contacts]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [c for c, _ in scored]


def _top5_outcome_rate(db, ranked_contacts: list) -> tuple[float | None, int]:
    top5 = ranked_contacts[:5]
    outcomes = [_most_recent_outcome(db, c.id) for c in top5]
    resolved = [o for o in outcomes if o is not None]
    if not resolved:
        return None, 0
    return sum(resolved) / len(resolved), len(resolved)


def run_backtest(db, cohort_id: str) -> BacktestResult:
    """Evaluate every lawyer in the given (already-generated) cohort."""
    user_tags = db.execute(
        select(models.DemoProvenance)
        .where(models.DemoProvenance.cohort_id == cohort_id,
               models.DemoProvenance.table_name == "users")
    ).scalars().all()
    users = db.execute(
        select(models.User).where(models.User.id.in_([t.row_id for t in user_tags]))
    ).scalars().all()

    v0_rates, v1_rates = [], []
    v0_n, v1_n = 0, 0
    for user in users:
        contact_tags = db.execute(
            select(models.DemoProvenance)
            .where(models.DemoProvenance.cohort_id == cohort_id,
                   models.DemoProvenance.table_name == "contacts")
        ).scalars().all()
        contacts = db.execute(
            select(models.Contact).where(
                models.Contact.id.in_([t.row_id for t in contact_tags]),
                models.Contact.user_id == user.id,
            )
        ).scalars().all()
        if not contacts:
            continue

        v0_ranked = _v0_naive_rank(db, contacts)
        v1_ranked = [t.contact_id for t in rt.rank_opportunities(db, user, contacts)]
        v1_ranked_contacts = sorted(contacts, key=lambda c: v1_ranked.index(c.id))

        v0_rate, v0_count = _top5_outcome_rate(db, v0_ranked)
        v1_rate, v1_count = _top5_outcome_rate(db, v1_ranked_contacts)
        if v0_rate is not None:
            v0_rates.append(v0_rate)
            v0_n += v0_count
        if v1_rate is not None:
            v1_rates.append(v1_rate)
            v1_n += v1_count

    return BacktestResult(
        n_lawyers_evaluated=len(users),
        v0_top5_outcome_rate=(sum(v0_rates) / len(v0_rates)) if v0_rates else 0.0,
        v1_top5_outcome_rate=(sum(v1_rates) / len(v1_rates)) if v1_rates else 0.0,
        v0_n_opportunities=v0_n, v1_n_opportunities=v1_n,
    )
