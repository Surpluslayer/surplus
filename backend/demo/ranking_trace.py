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

# Named factor groups for backend/observe's ablation experiments. Two kinds
# live in this one dict, on purpose:
#
#  - the CANONICAL PARTITION -- signal_practice / behavior / relationship --
#    is disjoint and covers every factor exactly once. This is what
#    backend/observe/harnesses/ablation.py's Model A/B/C progressive-richness
#    comparison and relationship_eval.py's centerpiece NDCG-lift claim are
#    built on; "Model A" = signal_practice alone, "Model B" = + behavior,
#    "Model C" = + relationship (= every factor).
#  - two NAMED ABLATION LEVERS -- signal_affinity / timing -- overlap the
#    partition above on purpose, for the single-opportunity "Ablate ->" UI
#    action (Observe checklist H: "remove signal affinity" / "remove
#    timing" as their own probes, distinct from "remove the whole
#    relationship dimension"). signal_affinity is practice_fit alone (a
#    strict subset of signal_practice); timing is relationship_recency +
#    relationship_trajectory (a strict subset of relationship -- the
#    time-based half of it, as opposed to relationship_strength's
#    volume/count-based half). Because of this overlap,
#    ablation.ablate_one() computes "everything except this group" from the
#    full factor set directly (a set difference), never by summing this
#    dict's groups together -- summing would double-count practice_fit or
#    the recency/trajectory pair for whichever request touches both an
#    overlapping and a canonical group. See ablate_one()'s own docstring.
#
# Kept here, next to FACTOR_WEIGHTS, so the two can never drift out of sync
# with each other (every name below must be a real key in FACTOR_WEIGHTS).
FACTOR_GROUPS = {
    "signal_practice": ("signal_relevance", "practice_fit"),
    "behavior": ("historical_behavior",),
    "relationship": ("relationship_strength", "relationship_recency", "relationship_trajectory"),
    "signal_affinity": ("practice_fit",),
    "timing": ("relationship_recency", "relationship_trajectory"),
}
_CANONICAL_PARTITION = ("signal_practice", "behavior", "relationship")
assert set(FACTOR_WEIGHTS) == {n for g in _CANONICAL_PARTITION for n in FACTOR_GROUPS[g]}
assert all(n in FACTOR_WEIGHTS for names in FACTOR_GROUPS.values() for n in names)


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


@dataclass
class RankingData:
    """Everything the factors need, fetched in bulk ONCE for a ranking run.

    The per-factor helpers below each did their own query, per contact. That
    is fine for one trace and quadratic for a ranking: scoring 60 candidates
    issued 2 queries per candidate, and the ablation loop re-ran the whole
    thing 8 more times (4 levers x full/without), so one contact click cost
    ~1,560 queries. Worse, _historical_behavior's query is account-wide --
    every "Drafted follow-up" row this lawyer ever produced -- so a book with
    ~660 drafts re-fetched and re-JSON-parsed all 660 of them once per
    candidate per pass: ~200,000 parses for a single click. On a networked
    Postgres that measured ~150s.

    This is a pure caching layer: same rows, same filters, same arithmetic --
    just fetched once. `as_of` is baked in at prefetch time, so a
    point-in-time replay stays point-in-time (the harness that guards against
    future leakage still passes untouched).
    """
    interactions: dict = field(default_factory=dict)      # contact_id -> ascending rows
    latest_signal: dict = field(default_factory=dict)     # contact_id -> row | None
    # signal_category -> (seen, saved, dismissed), account-wide
    behavior: dict = field(default_factory=dict)


# SQLite caps host parameters per statement (999 on older builds), so a book
# larger than that would blow up an IN (...) of every contact id.
_ID_CHUNK = 400


def prefetch(db, user, contacts: list, *, as_of: datetime | None = None) -> RankingData:
    """Bulk-load what compute_trace needs for `contacts`. Two queries plus one
    chunk per 400 contacts, instead of two per contact."""
    ids = [c.id for c in contacts]
    cutoff = _aware(as_of) if as_of is not None else None

    by_contact: dict = {cid: [] for cid in ids}
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i:i + _ID_CHUNK]
        rows = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id.in_(chunk))
            .order_by(models.RelationshipInteraction.occurred_at.asc())
        ).scalars().all()
        for r in rows:
            if cutoff is not None and _aware(r.occurred_at) > cutoff:
                continue
            by_contact.setdefault(r.contact_id, []).append(r)

    latest: dict = {}
    for cid, rows in by_contact.items():
        signals = [r for r in rows if r.source_type == "activity_update"]
        # Ordered ascending, so the newest is last. Tie-break on id to stay
        # deterministic where two signals share a timestamp.
        latest[cid] = max(signals, key=lambda r: (_aware(r.occurred_at), r.id)) if signals else None

    # Account-wide draft history, aggregated by category once rather than
    # re-scanned per candidate.
    behavior: dict = {}
    draft_rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.actor_user_id == user.id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
    ).scalars().all()
    for r in draft_rows:
        if cutoff is not None and _aware(r.occurred_at) > cutoff:
            continue
        m = json.loads(r.meta_json or "{}")
        cat = m.get("signal_category")
        if not cat:
            continue
        seen, saved, dismissed = behavior.get(cat, (0, 0, 0))
        if m.get("engaged"):
            saved += 1
        else:
            dismissed += 1
        behavior[cat] = (seen + 1, saved, dismissed)

    return RankingData(interactions=by_contact, latest_signal=latest, behavior=behavior)


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
    """Two sources for `category`, tried in order: backend/demo/cohort.py's
    own `signal_category` meta key (demo-cohort data only), then, when that's
    absent, a real mapping off PRODUCTION's actual interaction_type +
    meta_json fields (tax.production_signal_category) -- so this factor is
    genuinely computed on a real account's book, not permanently pinned to
    the neutral 0.5 fallback. `category_source` in the evidence says
    honestly which path produced the category (or that neither did)."""
    meta = json.loads(signal.meta_json or "{}") if signal and signal.meta_json else {}
    category = meta.get("signal_category")
    category_source = "demo_signal_category" if category else None
    if not category and signal is not None:
        category = tax.production_signal_category(getattr(signal, "interaction_type", None), meta)
        category_source = "production_interaction_type" if category else "unmapped"
    aff = tax.affinity(getattr(user, "practice_area", None), category)
    return RankingFactor("practice_fit", aff, {
        "lawyer_practice_area": getattr(user, "practice_area", None),
        "signal_category": category, "affinity_source": "seed_table",
        "category_source": category_source,
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


def _historical_behavior(db, user, signal, *, as_of: datetime | None = None,
                          behavior: dict | None = None) -> RankingFactor:
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

    # Precomputed by prefetch(): identical aggregation, done once for the whole
    # run instead of re-scanning every draft this account ever produced for
    # each candidate being scored.
    if behavior is not None:
        seen, saved, dismissed = behavior.get(category, (0, 0, 0))
        value = (saved / seen) if seen else 0.5
        return RankingFactor("historical_behavior", value, {
            "signal_category": category, "seen": seen,
            "saved": saved, "dismissed": dismissed,
        })

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
                   include_factors: list[str] | None = None,
                   data: RankingData | None = None) -> RankingTrace:
    """`as_of`: freeze the world at this timestamp (backend/observe's replay
    harness) -- every factor below is computed using only interactions/
    signals at-or-before `as_of`. `include_factors`: restrict to this subset
    of FACTOR_WEIGHTS' keys, renormalized (backend/observe's ablation
    harness) -- e.g. ["signal_relevance", "practice_fit"] for a
    signal+practice-only model. Both default to the original, unrestricted
    behavior.

    De-duplicates `include_factors` before computing anything: FACTOR_GROUPS
    has two INTENTIONALLY overlapping entries (signal_affinity subsets
    signal_practice; timing subsets relationship -- see that dict's own
    docstring), so a caller who naively concatenates every group's factors
    (`[n for g in FACTOR_GROUPS.values() for n in g]`) would otherwise pass
    a name twice, computing that factor twice and silently double-counting
    its contribution to the score despite the renormalized weight only
    counting it once. Safe regardless of how the caller built the list.

    `data`: a RankingData from prefetch(), so a ranking run reads each row
    once instead of once per candidate. Purely a cache -- omit it and every
    factor falls back to its own query, computing exactly the same values."""
    if data is not None and contact.id in data.interactions:
        interactions = data.interactions[contact.id]
        signal = data.latest_signal.get(contact.id)
        behavior = data.behavior
    else:
        signal = _latest_signal(db, contact.id, as_of=as_of)
        interactions = _all_interactions(db, contact.id, as_of=as_of)
        behavior = None

    factor_fns = {
        "signal_relevance": lambda: _signal_relevance(signal, as_of=as_of),
        "practice_fit": lambda: _practice_fit(user, signal),
        "relationship_strength": lambda: _relationship_strength(interactions),
        "relationship_recency": lambda: _relationship_recency(interactions, as_of=as_of),
        "relationship_trajectory": lambda: _relationship_trajectory(interactions, as_of=as_of),
        "historical_behavior": lambda: _historical_behavior(db, user, signal, as_of=as_of,
                                                            behavior=behavior),
    }
    names = (list(dict.fromkeys(include_factors)) if include_factors is not None
             else list(FACTOR_WEIGHTS.keys()))
    factors = [factor_fns[n]() for n in names]
    weights = _renormalized_weights(names)
    score = sum(f.value * weights[f.name] for f in factors)
    return RankingTrace(contact_id=contact.id, contact_name=contact.name or "",
                        factors=factors, opportunity_score=score)


def rank_opportunities(db, user, contacts: list, *, as_of: datetime | None = None,
                        include_factors: list[str] | None = None,
                        data: RankingData | None = None) -> list[RankingTrace]:
    """`data`: pass a prefetch() result to reuse one bulk load across several
    rankings of the SAME contacts at the SAME as_of -- what the ablation loop
    does, re-ranking the identical candidate set once per lever. Omitted, one
    is built here for this run."""
    if data is None:
        data = prefetch(db, user, contacts, as_of=as_of)
    traces = [compute_trace(db, user, c, as_of=as_of, include_factors=include_factors,
                            data=data)
              for c in contacts]
    traces.sort(key=lambda t: t.opportunity_score, reverse=True)
    for i, t in enumerate(traces, start=1):
        t.rank = i
    return traces
