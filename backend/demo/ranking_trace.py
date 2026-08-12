"""backend/demo/ranking_trace.py : the decision trace behind an opportunity
score -- the technical-observability object, not just the number.

Every RankingFactor below is independently computed and carries its own
`evidence` (the raw numbers a person can inspect), so a UI can render the
final score AND let someone click into any one factor and see exactly what
produced it. That's the point of building this as a trace object instead of
a single scoring function that returns a float: the moat claim isn't "we
have a good ranker", it's "you can inspect what the ranker actually used".

Two factors are REAL production logic reused directly, not reimplemented:
  - relationship_strength / relationship_recency: computed from the same
    interaction-counting math book.py's own read-model uses conceptually
    (recency + frequency), against real generated RelationshipInteraction
    rows -- not a fabricated number per contact.
  - signal_relevance's freshness curve mirrors updates_engine's own
    "detected signal" concept (source_type == "activity_update").

Two factors are the demo-only "NEXT SURPLUS" layer, clearly marked:
  - practice_fit: signal_taxonomy.affinity() -- the seed table, honestly
    labeled as seed, not learned.
  - historical_behavior: EMPIRICALLY computed from this lawyer's own past
    generated engagement (meta_json on prior drafts), not the seed table --
    this is the one factor that's genuinely "accumulated", by construction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from . import signal_taxonomy as tax
from .. import models

_UTC = timezone.utc

# Documented, not tuned against anything -- see module docstring. Change
# these and every trace recomputes consistently; nothing hardcodes a weight
# inside the per-factor functions below.
FACTOR_WEIGHTS = {
    "signal_relevance": 0.20,
    "practice_fit": 0.20,
    "relationship_strength": 0.20,
    "relationship_recency": 0.15,
    "relationship_trajectory": 0.10,
    "historical_behavior": 0.15,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9

# Named, additive factor groups for progressive-richness experiments
# (backend/observe/harnesses/ablation.py, relationship_eval.py). "Model A" =
# signal_practice alone; "Model B" = + behavior; "Model C" = + relationship.
# Kept here, next to FACTOR_WEIGHTS, so the two can never drift out of sync
# with each other (every name below must be a real key in FACTOR_WEIGHTS).
FACTOR_GROUPS = {
    "signal_practice": ("signal_relevance", "practice_fit"),
    "behavior": ("historical_behavior",),
    "relationship": ("relationship_strength", "relationship_recency", "relationship_trajectory"),
}
assert set(FACTOR_WEIGHTS) == {n for names in FACTOR_GROUPS.values() for n in names}


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


@dataclass
class RankingFactor:
    name: str
    value: float  # 0-1
    evidence: dict = field(default_factory=dict)


@dataclass
class RankingTrace:
    contact_id: int
    contact_name: str
    factors: list[RankingFactor]
    opportunity_score: float
    rank: int | None = None

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "contact_name": self.contact_name,
            "opportunity_score": round(self.opportunity_score, 3),
            "rank": self.rank,
            "factors": [
                {"name": f.name, "value": round(f.value, 3),
                 "weight": FACTOR_WEIGHTS.get(f.name), "evidence": f.evidence}
                for f in self.factors
            ],
        }


def _latest_signal(db, contact_id: int, *, as_of: datetime | None = None):
    """Most recent detected-signal interaction on this contact (real
    source_type taxonomy: 'activity_update'). `as_of`, when given, restricts
    to signals at-or-before that timestamp -- filtered in Python against the
    normalized (_aware) occurred_at, not in SQL, for the same reason
    accounts_read.py normalizes post-fetch: SQLite hands back naive values
    even when tz-aware ones were stored, and a DB-side comparison against a
    naive column would silently miscompare an aware bind parameter."""
    rows = list(db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact_id,
               models.RelationshipInteraction.source_type == "activity_update")
        .order_by(models.RelationshipInteraction.occurred_at.desc())
    ).scalars().all())
    if as_of is not None:
        cutoff = _aware(as_of)
        rows = [r for r in rows if _aware(r.occurred_at) <= cutoff]
    return rows[0] if rows else None


def _all_interactions(db, contact_id: int, *, as_of: datetime | None = None) -> list:
    rows = list(db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact_id)
        .order_by(models.RelationshipInteraction.occurred_at.asc())
    ).scalars().all())
    if as_of is not None:
        cutoff = _aware(as_of)
        rows = [r for r in rows if _aware(r.occurred_at) <= cutoff]
    return rows


def _signal_relevance(signal, *, as_of: datetime | None = None) -> RankingFactor:
    if signal is None:
        return RankingFactor("signal_relevance", 0.0, {"reason": "no detected signal on this contact"})
    now = _aware(as_of) if as_of is not None else datetime.now(_UTC)
    age_days = (now - _aware(signal.occurred_at)).days
    # Freshness decays over 2 weeks to ~0.1, floors there rather than hitting
    # exactly zero (an old-but-real signal still has SOME relevance).
    value = max(0.1, 1.0 - (age_days / 14.0))
    meta = json.loads(signal.meta_json or "{}") if hasattr(signal, "meta_json") else {}
    return RankingFactor("signal_relevance", value, {
        "signal_age_days": age_days, "signal_title": signal.title,
        "signal_category": meta.get("signal_category"),
    })


def _practice_fit(user, signal) -> RankingFactor:
    meta = json.loads(signal.meta_json or "{}") if signal and signal.meta_json else {}
    category = meta.get("signal_category")
    aff = tax.affinity(getattr(user, "practice_area", None), category)
    return RankingFactor("practice_fit", aff, {
        "lawyer_practice_area": getattr(user, "practice_area", None),
        "signal_category": category, "affinity_source": "seed_table",
    })


def _relationship_strength(interactions: list) -> RankingFactor:
    n = len(interactions)
    # Normalize against 20 interactions -> 1.0, matching the eval example's
    # "14 historical interactions" landing well below the ceiling.
    value = min(1.0, n / 20.0)
    return RankingFactor("relationship_strength", value, {"total_interactions": n})


def _relationship_recency(interactions: list, *, as_of: datetime | None = None) -> RankingFactor:
    if not interactions:
        return RankingFactor("relationship_recency", 0.0, {"last_interaction_days": None})
    last = _aware(interactions[-1].occurred_at)
    now = _aware(as_of) if as_of is not None else datetime.now(_UTC)
    days = (now - last).days
    value = max(0.0, 1.0 - (days / 30.0))
    return RankingFactor("relationship_recency", value, {"last_interaction_days": days})


def _relationship_trajectory(interactions: list, *, as_of: datetime | None = None) -> RankingFactor:
    now = _aware(as_of) if as_of is not None else datetime.now(_UTC)
    last_30 = sum(1 for i in interactions if (now - _aware(i.occurred_at)).days <= 30)
    prior_30 = sum(1 for i in interactions if 30 < (now - _aware(i.occurred_at)).days <= 60)
    if prior_30 == 0:
        trend = 1.0 if last_30 > 0 else 0.0
    else:
        trend = (last_30 - prior_30) / max(last_30, prior_30)
    value = max(0.0, min(1.0, (trend + 1) / 2))  # map [-1,1] -> [0,1]
    return RankingFactor("relationship_trajectory", value, {
        "interactions_last_30d": last_30, "interactions_prior_30d": prior_30,
        "trend": round(trend, 3),
    })


def _historical_behavior(db, user, signal, *, as_of: datetime | None = None) -> RankingFactor:
    """EMPIRICAL, not seed: how has THIS lawyer actually engaged with drafts
    on this signal_category historically? Computed from meta_json on every
    prior 'Drafted follow-up' interaction this lawyer generated -- the one
    factor in this trace that is genuinely 'accumulated data', by
    construction, matching the moat brief's 'signal performance' concept.

    `as_of`, when given, excludes any draft occurring AFTER that timestamp --
    without this filter a point-in-time replay would silently "know" the
    disposition of drafts written after the replay's own cutoff, which is
    exactly the future-leakage failure mode
    backend/observe/harnesses/replay.py exists to catch and its own test
    exists to prove doesn't happen."""
    meta = json.loads(signal.meta_json or "{}") if signal and signal.meta_json else {}
    category = meta.get("signal_category")
    if not category:
        return RankingFactor("historical_behavior", 0.5, {"reason": "no signal category to compare against"})

    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.actor_user_id == user.id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
    ).scalars().all()
    if as_of is not None:
        cutoff = _aware(as_of)
        rows = [r for r in rows if _aware(r.occurred_at) <= cutoff]
    seen = saved = dismissed = 0
    for r in rows:
        m = json.loads(r.meta_json or "{}")
        if m.get("signal_category") != category:
            continue
        seen += 1
        if m.get("engaged"):
            saved += 1
        else:
            dismissed += 1
    value = (saved / seen) if seen else 0.5
    return RankingFactor("historical_behavior", value, {
        "signal_category": category, "seen": seen, "saved": saved, "dismissed": dismissed,
    })


def _renormalized_weights(names: list[str]) -> dict:
    """Ablation-safe weighting: when a factor group is excluded, its weight
    is NOT silently dropped (which would just shrink every score toward 0
    and make cross-model scores incomparable) -- the remaining factors'
    weights are renormalized to sum to 1.0, so a restricted-factor-set score
    is still a fair 0-1 opportunity score, comparable across ablation
    models. With every factor included this is a no-op (returns
    FACTOR_WEIGHTS unchanged), which is why the default compute_trace() call
    below produces byte-identical scores to before this function existed."""
    subset = {k: v for k, v in FACTOR_WEIGHTS.items() if k in names}
    total = sum(subset.values())
    return {k: v / total for k, v in subset.items()}


def compute_trace(db, user, contact, *, as_of: datetime | None = None,
                   include_factors: list[str] | None = None) -> RankingTrace:
    """`as_of`: freeze the world at this timestamp (backend/observe's replay
    harness) -- every factor below is computed using only interactions/
    signals at-or-before `as_of`. `include_factors`: restrict to this subset
    of FACTOR_WEIGHTS' keys, renormalized (backend/observe's ablation
    harness) -- e.g. ["signal_relevance", "practice_fit"] for a
    signal+practice-only model. Both default to the original, unrestricted
    behavior."""
    signal = _latest_signal(db, contact.id, as_of=as_of)
    interactions = _all_interactions(db, contact.id, as_of=as_of)

    factor_fns = {
        "signal_relevance": lambda: _signal_relevance(signal, as_of=as_of),
        "practice_fit": lambda: _practice_fit(user, signal),
        "relationship_strength": lambda: _relationship_strength(interactions),
        "relationship_recency": lambda: _relationship_recency(interactions, as_of=as_of),
        "relationship_trajectory": lambda: _relationship_trajectory(interactions, as_of=as_of),
        "historical_behavior": lambda: _historical_behavior(db, user, signal, as_of=as_of),
    }
    names = include_factors if include_factors is not None else list(FACTOR_WEIGHTS.keys())
    factors = [factor_fns[n]() for n in names]
    weights = _renormalized_weights(names)
    score = sum(f.value * weights[f.name] for f in factors)
    return RankingTrace(contact_id=contact.id, contact_name=contact.name or "",
                        factors=factors, opportunity_score=score)


def rank_opportunities(db, user, contacts: list, *, as_of: datetime | None = None,
                        include_factors: list[str] | None = None) -> list[RankingTrace]:
    traces = [compute_trace(db, user, c, as_of=as_of, include_factors=include_factors)
              for c in contacts]
    traces.sort(key=lambda t: t.opportunity_score, reverse=True)
    for i, t in enumerate(traces, start=1):
        t.rank = i
    return traces
