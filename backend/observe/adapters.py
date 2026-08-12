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
        decision="relevant" if sum(f.value for f in rel_factors) > 0 else "not yet established",
        score=(sum(f.value for f in rel_factors) / len(rel_factors)) if rel_factors else None,
        versions=dict(ver.VERSIONS),
        provenance=_provenance_for(user),
        evaluation_references=[{"harness_id": "relationship_evaluation", "version": "v1"},
                                {"harness_id": "historical_replay", "version": "v1"}],
    )


# ── Opportunity ──────────────────────────────────────────────────────────

def opportunity_trace(db, user: models.User, contact: models.Contact, candidates: list) -> DecisionTrace:
    """Why did this opportunity rank where it ranked?"""
    ranked = rt.rank_opportunities(db, user, candidates)
    this = next((t for t in ranked if t.contact_id == contact.id), None)
    if this is None:
        this = rt.compute_trace(db, user, contact)
    return DecisionTrace(
        account_id=user.id,
        object_type="opportunity", object_id=str(contact.id),
        timestamp=trace_now(),
        inputs={"candidate_set_size": len(candidates)},
        entities={"contact_name": contact.name, "user_email": user.email},
        features=_factors_to_features(this.factors),
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
