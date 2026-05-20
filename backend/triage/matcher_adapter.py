"""
triage/matcher_adapter.py : duck-typed bridge so backend/agents/matcher
+ backend/agents/roi can chew on Applicant rows without schema changes
or matcher/ROI edits.

Gap closures from the report:
  - G1: Applicant rows lack .side / .works_on / .seniority / .offers /
    .seeks / .fit_score-as-attribute. Synthesize the missing fields
    from ApplicantEvaluation + sensible defaults (.archetype → .side
    + .works_on mapping; constant seniority; empty offers/seeks).
  - G2: inbound has no `.status == "rsvp"` semantics. We treat
    ReviewDecision.human_decision == "accept" as "rsvp"; fall back to
    ApplicantEvaluation.recommendation == "accept" so demos without
    operator review still produce a non-empty attending list.

The two helpers `run_inbound_match` and `run_inbound_roi` keep the
route-side branching one if-check long.
"""
from __future__ import annotations
from types import SimpleNamespace

from ..agents.matcher import build_edges, form_groups
from ..agents.roi import settle


# Gap #1 / smoke #7: archetype → side. Builds / Hires / Operates is the
# taxonomy matcher.build_edges keys off for cross-side (symbiotic) edges.
# We spread the 10 archetypes across all three sides so mixed-archetype
# pools (the realistic case for a founders / partners CSV) produce a
# meaningful number of cross-side pairs in the heuristic matcher path.
# Homogeneous pools (e.g., all "founder") still produce only affinity
# edges — that's a fundamental matcher limit, not an adapter one. The
# LLM-driven matcher_lib path bypasses this taxonomy entirely when
# ANTHROPIC_API_KEY is set.
_ARCHETYPE_TO_SIDE = {
    "founder":          "Builds",
    "engineer":         "Builds",
    "researcher":       "Builds",
    "creator":          "Builds",
    "operator":         "Operates",
    "service_provider": "Operates",
    "community_member": "Operates",
    "student":          "Operates",   # was Builds — students are a distinct cohort
    "investor":         "Hires",
    "other":            "Hires",      # was Builds — spreads the "other" bucket off-side
}

# Gap #1 / smoke #7: archetype → works_on. matcher._AFFINITY adjacency
# keys are domain tags. Varying tags per archetype increases the chance
# of affinity edges within same-side pools (which matters when the
# heuristic falls back to affinity-only for homogeneous-archetype CSVs).
_ARCHETYPE_TO_WORKS_ON = {
    "founder":          "general",
    "engineer":         "web-infra",
    "researcher":       "ml-platform",
    "creator":          "web-infra",
    "operator":         "general",
    "service_provider": "data-infra",
    "community_member": "general",
    "student":          "general",
    "investor":         "general",
    "other":            "general",
}


def _accepted_applicants(applicants):
    """Gap #2: inbound's `.status == 'rsvp'` analogue.

    Primary signal : a ReviewDecision row with human_decision == 'accept'.
    Fallback (for first-run demos before any manual review) : the
    model's own recommendation == 'accept'. Same shape, so the rest of
    the adapter doesn't care which fired.
    """
    decided = [a for a in applicants
               if a.decision and a.decision.human_decision == "accept"]
    if decided:
        return decided
    return [a for a in applicants
            if a.evaluation and a.evaluation.recommendation == "accept"]


def applicants_as_attending(event):
    """Gap #1+#2: return matcher/ROI-shaped duck objects for inbound events.

    Each object exposes the attribute surface matcher.py + roi.py
    actually read : id, name, role, company, linkedin_url, side,
    works_on, seniority, offers, seeks, fit_score, status, group_id,
    outreach, conversion.

    `id` reuses Applicant.id. Safe because the route-side guard skips
    persisting MatchEdge / SponsorMatch / Conversion rows for inbound
    events (those tables FK to prospects.id, see G3a).
    """
    accepted = _accepted_applicants(list(event.applicants or []))
    out = []
    for a in accepted:
        ev = a.evaluation
        archetype = (ev.archetype if ev else "other") or "other"
        out.append(SimpleNamespace(
            id=a.id,
            name=a.name or f"Applicant {a.id}",
            role=a.role or "",
            company=a.company or "",
            linkedin_url=a.linkedin_url or "",
            side=_ARCHETYPE_TO_SIDE.get(archetype, "Builds"),
            works_on=_ARCHETYPE_TO_WORKS_ON.get(archetype, "general"),
            # Gap #1 default : Applicant has no seniority column and the
            # triage scorer doesn't extract one. "Senior" maps to
            # matcher_lib's "advanced" exp_level which is the most common
            # case for the operator-curated applicant pools we see.
            seniority="Senior",
            offers="",
            seeks="",
            fit_score=ev.fit_score if ev else 0,
            status="rsvp",
            group_id=None,
            outreach=[],
            conversion=None,
        ))
    return out


def is_inbound_event(event) -> bool:
    """One-place definition of "this event runs through the triage path"."""
    return bool(event.triage_config) and not list(event.prospects or [])


def run_inbound_match(event):
    """Gap #3a: in-memory match for inbound. No DB writes (MatchEdge /
    SponsorMatch are FK-bound to prospects.id; applicant rows aren't
    prospects).

    Returns (attending, edges, groups) or None when no accepted
    applicants exist yet.
    """
    attending = applicants_as_attending(event)
    if not attending:
        return None
    edges = build_edges(attending, event=event)
    groups = form_groups(attending, event)
    return attending, edges, groups


def run_inbound_roi(event):
    """Gap #3a: in-memory ROI ledger for inbound. No Conversion writes
    (FK on prospects.id).

    Returns (attending, ledger, metrics) or None when no accepted
    applicants exist yet.
    """
    attending = applicants_as_attending(event)
    if not attending:
        return None
    ledger, metrics = settle(event, attending)
    return attending, ledger, metrics
