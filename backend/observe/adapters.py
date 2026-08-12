"""backend/observe/adapters.py : object-specific adapters producing the
universal DecisionTrace (trace.py) for every "click X -> why?" surface the
Observe spec lists. Each adapter reshapes an EXISTING, real computation
(ranking_trace.compute_trace, solicitation_signals.check,
signal_taxonomy.affinity) into the universal trace format -- it never
re-derives or invents a second explanation for the same decision (see this
package's docstring: "The panel should not have a completely separate
explanation system for each feature").

    Existing Surplus Object -> Observability Adapter -> DecisionTrace -> Panel
"""
from __future__ import annotations

import json

from . import versions as ver
from .trace import DecisionTrace, TraceFeature, DEMO, OBSERVED, now as trace_now
from .. import models
from ..agents.relationship import solicitation_signals as sig
from ..demo import ranking_trace as rt


def _provenance_for(user) -> str:
    """A generated demo user is provenance=DEMO; a real signed-in Surplus
    account is provenance=OBSERVED. SYNTHETIC is not reachable through this
    function -- synthetic scenarios build their own traces directly (see
    harnesses/synthetic_scenarios.py), never through an adapter that implies
    they came from an account."""
    return DEMO if getattr(user, "is_demo", False) else OBSERVED


def _factors_to_features(factors) -> list:
    return [TraceFeature(name=f.name, value=round(f.value, 4), evidence=f.evidence,
                          weight=rt.FACTOR_WEIGHTS.get(f.name))
            for f in factors]


# ── Signal ───────────────────────────────────────────────────────────────

def signal_trace(db, interaction: models.RelationshipInteraction) -> DecisionTrace:
    """Why was this event classified as this signal?"""
    meta = json.loads(interaction.meta_json or "{}")
    user = db.get(models.User, interaction.actor_user_id)
    return DecisionTrace(
        account_id=interaction.actor_user_id,
        object_type="signal", object_id=str(interaction.id),
        timestamp=interaction.occurred_at,
        inputs={"title": interaction.title, "summary": interaction.summary},
        entities={"contact_id": interaction.contact_id},
        features=[
            TraceFeature(name="signal_kind", value=meta.get("signal_kind"),
                         evidence={"source": "updates_engine._DRAFTWORTHY_KINDS (real, production)"}),
            TraceFeature(name="signal_category", value=meta.get("signal_category"),
                         evidence={"source": "demo/signal_taxonomy.py (demo-only, no production "
                                              "classifier for this level yet)"}),
        ],
        decision=f"classified as {meta.get('signal_category') or 'unclassified'}",
        versions=dict(ver.VERSIONS),
        outcome={"engaged": meta.get("engaged"), "disposition": meta.get("disposition")},
        provenance=_provenance_for(user) if user else DEMO,
        evaluation_references=[{"harness_id": "signal_library_evaluation", "version": "v1"}],
    )


# ── Signal Library ───────────────────────────────────────────────────────

def signal_library_trace(db, account_id: int, cohort_id: str, category: str) -> DecisionTrace:
    """Why does this event belong to this library?"""
    from .harnesses import signal_library_eval as sle
    funnel = sle.library_funnel(db, cohort_id).get(category, {"detected": 0, "engaged": 0, "sent": 0})
    return DecisionTrace(
        account_id=account_id,
        object_type="signal_library", object_id=category,
        timestamp=trace_now(),
        inputs={"cohort_id": cohort_id, "category": category},
        features=[TraceFeature(name="funnel", value=None, evidence=funnel)],
        decision=f"{category} library membership",
        versions=dict(ver.VERSIONS),
        outcome=funnel,
        provenance=DEMO,
        evaluation_references=[{"harness_id": "signal_library_evaluation", "version": "v1"}],
    )


# ── Lawyer (targeting) ───────────────────────────────────────────────────

def targeting_trace(db, user: models.User, contact: models.Contact) -> DecisionTrace:
    """Why did this lawyer receive it?"""
    trace = rt.compute_trace(db, user, contact)
    relevant = [f for f in trace.factors if f.name in ("signal_relevance", "practice_fit")]
    return DecisionTrace(
        account_id=user.id,
        object_type="lawyer", object_id=str(user.id),
        timestamp=trace_now(),
        inputs={"contact_id": contact.id, "practice_area": getattr(user, "practice_area", None)},
        entities={"user_email": user.email, "contact_name": contact.name},
        features=_factors_to_features(relevant),
        decision="targeted" if trace.opportunity_score > 0 else "not targeted",
        score=trace.opportunity_score,
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user),
        evaluation_references=[{"harness_id": "signal_library_evaluation", "version": "v1"},
                                {"harness_id": "ablation", "version": "v1"}],
    )


# ── Relationship ─────────────────────────────────────────────────────────

def _relationship_context(db, user: models.User, contact: models.Contact) -> dict:
    """Supplementary, UNSCORED relationship evidence -- real and computable
    from the current schema, but deliberately NOT folded into
    ranking_trace's weighted factors (that scoring system is already tested
    and relied on by the relationship-evaluation harness; adding new scored
    dimensions here would silently reweight it as a side effect of an
    Observe-only change). Two fields, both honestly derived:

      - relationship_duration_days: age of the FIRST recorded interaction --
        real, from RelationshipInteraction rows.
      - shared_entities: how many of this lawyer's OTHER contacts share this
        contact's company -- a real, computable proxy for "shared entities",
        not the richer shared_matter/topic overlap the original spec
        described (this schema has no matter or topic concept to draw that
        from -- see the note below rather than fabricating one).

    topic_overlap and practice_overlap (both named in the original Observe
    brief) are NOT computed here: this schema has no topic taxonomy at the
    interaction level beyond the demo-only signal_category (already
    captured by historical_behavior), and "practice overlap" has no
    real meaning for a lawyer <-> non-lawyer Contact pair -- practice_fit
    (signal-category affinity) is the closest real analog and is already a
    separate, scored factor."""
    interactions = rt._all_interactions(db, contact.id)
    duration_days = None
    if interactions:
        first = interactions[0].occurred_at
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        first_aware = first.replace(tzinfo=timezone.utc) if first.tzinfo is None else first
        duration_days = (now - first_aware).days

    shared_entities = 0
    if contact.company:
        from sqlalchemy import select as _select, func as _func
        shared_entities = db.execute(
            _select(_func.count(models.Contact.id))
            .where(models.Contact.user_id == user.id, models.Contact.company == contact.company,
                   models.Contact.id != contact.id)
        ).scalar_one()

    return {
        "relationship_duration_days": duration_days,
        "shared_entities": shared_entities,
        "shared_entities_basis": "other contacts in this lawyer's book at the same company",
        "topic_overlap": None,
        "practice_overlap": None,
        "not_computed_reason": ("topic_overlap/practice_overlap: no topic or lawyer-practice "
                                "taxonomy exists at the Contact level in this schema"),
    }


def relationship_trace(db, user: models.User, contact: models.Contact) -> DecisionTrace:
    """Why is this person relevant?"""
    trace = rt.compute_trace(db, user, contact)
    rel_names = ("relationship_strength", "relationship_recency", "relationship_trajectory")
    rel_factors = [f for f in trace.factors if f.name in rel_names]
    return DecisionTrace(
        account_id=user.id,
        object_type="relationship", object_id=str(contact.id),
        timestamp=trace_now(),
        inputs={"contact_id": contact.id},
        entities={"contact_name": contact.name, "user_email": user.email},
        features=_factors_to_features(rel_factors),
        evidence=_relationship_context(db, user, contact),
        decision="relevant" if sum(f.value for f in rel_factors) > 0 else "not yet established",
        score=(sum(f.value for f in rel_factors) / len(rel_factors)) if rel_factors else None,
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user),
        evaluation_references=[{"harness_id": "relationship_evaluation", "version": "v1"},
                                {"harness_id": "historical_replay", "version": "v1"}],
    )


# ── Opportunity ──────────────────────────────────────────────────────────

# Cap on the candidate list embedded in one opportunity trace's evidence --
# a lawyer's full book can run into the hundreds; the panel only ever needs
# the ranking context around the selected candidate, not every row.
MAX_CANDIDATES_IN_EVIDENCE = 30


def _candidate_comparison(this, ranked: list) -> dict | None:
    """Why did the selected candidate beat (or lose to) its nearest
    alternative? Compares against the ADJACENT rank, not every other
    candidate -- the adjacent one is the actual competitor that determined
    whether this candidate is #1 (or wherever it landed): the candidate it
    beat (rank+1) if this one is on top, or the one it lost to (rank-1)
    otherwise."""
    if this.rank is None or len(ranked) < 2:
        return None
    if this.rank == 1:
        alt = next((t for t in ranked if t.rank == 2), None)
        beat_alternative = True
    else:
        alt = next((t for t in ranked if t.rank == this.rank - 1), None)
        beat_alternative = False
    if alt is None:
        return None

    this_by_name = {f.name: f.value for f in this.factors}
    alt_by_name = {f.name: f.value for f in alt.factors}
    factor_deltas = [
        {"factor": name, "this": round(this_by_name[name], 4),
         "alternative": round(alt_by_name.get(name, 0.0), 4),
         "delta": round(this_by_name[name] - alt_by_name.get(name, 0.0), 4)}
        for name in this_by_name
    ]
    factor_deltas.sort(key=lambda d: abs(d["delta"]), reverse=True)

    return {
        "alternative_contact_id": alt.contact_id, "alternative_contact_name": alt.contact_name,
        "alternative_rank": alt.rank, "alternative_score": round(alt.opportunity_score, 4),
        "this_beat_alternative": beat_alternative,
        "score_delta": round(this.opportunity_score - alt.opportunity_score, 4),
        "factor_deltas": factor_deltas,
        "largest_contributing_factor": factor_deltas[0]["factor"] if factor_deltas else None,
    }


def opportunity_trace(db, user: models.User, contact: models.Contact, candidates: list) -> DecisionTrace:
    """Why did this opportunity rank where it ranked? -- full candidate set
    considered (Observe checklist D) plus a factor-level comparison against
    the alternative it beat or lost to (Observe checklist D's "why did the
    selected candidate beat the alternatives")."""
    ranked = rt.rank_opportunities(db, user, candidates)
    this = next((t for t in ranked if t.contact_id == contact.id), None)
    if this is None:
        this = rt.compute_trace(db, user, contact)
        ranked = [this]

    return DecisionTrace(
        account_id=user.id,
        object_type="opportunity", object_id=str(contact.id),
        timestamp=trace_now(),
        inputs={"candidate_set_size": len(candidates)},
        entities={"contact_name": contact.name, "user_email": user.email},
        features=_factors_to_features(this.factors),
        evidence={
            "candidates_considered": [
                {"contact_id": t.contact_id, "contact_name": t.contact_name,
                 "rank": t.rank, "score": round(t.opportunity_score, 4)}
                for t in ranked[:MAX_CANDIDATES_IN_EVIDENCE]
            ],
            "candidates_considered_total": len(ranked),
            "comparison": _candidate_comparison(this, ranked),
        },
        decision=f"ranked #{this.rank}" if this.rank else "unranked",
        score=this.opportunity_score, rank=this.rank,
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user),
        evaluation_references=[{"harness_id": "ablation", "version": "v1"},
                                {"harness_id": "relationship_evaluation", "version": "v1"},
                                {"harness_id": "historical_replay", "version": "v1"}],
    )


# ── Draft ────────────────────────────────────────────────────────────────

def draft_trace(db, interaction: models.RelationshipInteraction) -> DecisionTrace:
    """Where did these facts/context come from?"""
    meta = json.loads(interaction.meta_json or "{}")
    user = db.get(models.User, interaction.actor_user_id)
    contact = db.get(models.Contact, interaction.contact_id) if interaction.contact_id else None
    return DecisionTrace(
        account_id=interaction.actor_user_id,
        object_type="draft", object_id=str(interaction.id),
        timestamp=interaction.occurred_at,
        inputs={"signal_kind": meta.get("signal_kind"), "signal_category": meta.get("signal_category")},
        entities={"contact_name": contact.name if contact else None},
        features=[
            TraceFeature(name="signal_source", value=meta.get("signal_kind"),
                         evidence={"row_id": interaction.id}),
            TraceFeature(name="practice_area", value=meta.get("practice_area"), evidence={}),
            TraceFeature(name="affinity_seed", value=meta.get("affinity_seed"),
                         evidence={"source": "demo/signal_taxonomy.SIGNAL_AFFINITY_SEED"}),
        ],
        decision=interaction.summary or "",
        outcome={"engaged": meta.get("engaged"), "disposition": meta.get("disposition")},
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user) if user else DEMO,
        evaluation_references=[{"harness_id": "historical_replay", "version": "v1"}],
    )


# ── Jurisdiction ─────────────────────────────────────────────────────────

def jurisdiction_trace(db, user: models.User, contact: models.Contact,
                        channel: str = "linkedin_dm") -> DecisionTrace:
    """Which rule allowed/blocked this?"""
    verdict = sig.check(db, user, contact, channel)
    return DecisionTrace(
        account_id=user.id,
        object_type="jurisdiction", object_id=f"{user.id}:{contact.id}:{channel}",
        timestamp=trace_now(),
        inputs={"jurisdiction": getattr(user, "bar_jurisdiction", None), "channel": channel,
                "relationship_type": getattr(contact, "relationship_type", None)},
        entities={"user_email": user.email, "contact_name": contact.name},
        features=[TraceFeature(name="verdict_reason", value=verdict.reason)],
        decision="PASS" if verdict.allowed else "BLOCK",
        policy_result={"allowed": verdict.allowed, "reason": verdict.reason,
                       "requires_disclosure_label": verdict.requires_disclosure_label},
        constraints={"jurisdiction_policy_version": ver.JURISDICTION_POLICY_VERSION},
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user),
        evaluation_references=[{"harness_id": "jurisdiction_regression", "version": "v1"}],
    )


# ── Outcome ──────────────────────────────────────────────────────────────

def outcome_trace(db, interaction: models.RelationshipInteraction) -> DecisionTrace:
    """How does this become evaluation data?"""
    meta = json.loads(interaction.meta_json or "{}")
    user = db.get(models.User, interaction.actor_user_id)
    return DecisionTrace(
        account_id=interaction.actor_user_id,
        object_type="outcome", object_id=str(interaction.id),
        timestamp=interaction.occurred_at,
        entities={"contact_id": interaction.contact_id},
        features=[TraceFeature(name="disposition", value=meta.get("disposition"), evidence=meta)],
        decision=meta.get("disposition") or "unresolved",
        outcome={"engaged": meta.get("engaged"), "disposition": meta.get("disposition")},
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user) if user else DEMO,
        evaluation_references=[{"harness_id": "relationship_evaluation", "version": "v1"},
                                {"harness_id": "historical_replay", "version": "v1"}],
    )
