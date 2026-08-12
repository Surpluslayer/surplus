"""backend/demo/signal_taxonomy.py : practice-area x signal-category affinity.

This is the "NEXT SURPLUS" layer, not a reproduction of anything in
production -- there is no practice_area or signal_category concept in the
live product today (confirmed: no such fields/tables exist outside what
this session added). Every value here is a SEED, not a learned one -- there
is no real outcome history to derive it from yet.

The point of building it as a table instead of a hardcoded weight inside a
scoring function: the moat this is meant to demonstrate isn't "we picked
good weights", it's "this affinity is queryable and re-derivable from real
outcomes the moment they exist" -- see update_affinity_from_outcomes()
below, which recomputes it from generated engagement data (a stand-in for
real outcome data, same posture as extended.py's outcome_feedback layer).
"""
from __future__ import annotations

PRACTICE_AREAS = (
    "corporate_ma", "litigation", "real_estate", "employment_labor",
    "ip", "regulatory",
)

# Coarse signal kind (real, matches updates_engine._DRAFTWORTHY_KINDS) ->
# finer-grained category (demo-only, legal-BD specific).
SIGNAL_CATEGORIES = {
    "job_change": ("gc_appointment", "exec_appointment", "founder_departure"),
    "new_post": ("acquisition_announced", "funding_announcement",
                 "litigation_filed", "regulatory_action", "product_launch"),
}
ALL_SIGNAL_CATEGORIES = tuple(c for cats in SIGNAL_CATEGORIES.values() for c in cats)

# practice_area -> signal_category -> affinity in [0, 1]. SEED values: a
# reasonable starting prior (an M&A lawyer plausibly cares more about an
# acquisition announcement than a litigation lawyer does), not a measurement.
SIGNAL_AFFINITY_SEED: dict[str, dict[str, float]] = {
    "corporate_ma": {
        "gc_appointment": 0.85, "exec_appointment": 0.55, "founder_departure": 0.50,
        "acquisition_announced": 0.95, "funding_announcement": 0.70,
        "litigation_filed": 0.20, "regulatory_action": 0.40, "product_launch": 0.30,
    },
    "litigation": {
        "gc_appointment": 0.45, "exec_appointment": 0.30, "founder_departure": 0.35,
        "acquisition_announced": 0.25, "funding_announcement": 0.20,
        "litigation_filed": 0.90, "regulatory_action": 0.60, "product_launch": 0.15,
    },
    "real_estate": {
        "gc_appointment": 0.35, "exec_appointment": 0.30, "founder_departure": 0.20,
        "acquisition_announced": 0.55, "funding_announcement": 0.40,
        "litigation_filed": 0.30, "regulatory_action": 0.45, "product_launch": 0.20,
    },
    "employment_labor": {
        "gc_appointment": 0.50, "exec_appointment": 0.65, "founder_departure": 0.55,
        "acquisition_announced": 0.35, "funding_announcement": 0.25,
        "litigation_filed": 0.55, "regulatory_action": 0.50, "product_launch": 0.15,
    },
    "ip": {
        "gc_appointment": 0.40, "exec_appointment": 0.30, "founder_departure": 0.20,
        "acquisition_announced": 0.45, "funding_announcement": 0.50,
        "litigation_filed": 0.40, "regulatory_action": 0.25, "product_launch": 0.75,
    },
    "regulatory": {
        "gc_appointment": 0.55, "exec_appointment": 0.35, "founder_departure": 0.25,
        "acquisition_announced": 0.40, "funding_announcement": 0.30,
        "litigation_filed": 0.50, "regulatory_action": 0.90, "product_launch": 0.20,
    },
}


# Production's REAL interaction_type/meta_json vocabulary (confirmed by
# direct trace of agents/relationship/updates_engine.py's _emit call sites --
# never signal_category/signal_kind, which is demo-cohort-only): job_change
# rows carry new_title/new_company/prev_company; new_post rows carry
# milestone_type. Mapped here onto this table's EXISTING fine-grained
# categories so practice_fit is computed the same way regardless of whether
# the underlying row came from backend/demo/cohort.py or a real production
# detection sweep -- one affinity table, two ways to arrive at a category.
_JOB_TITLE_KEYWORDS = (
    (("general counsel", " gc ", "gc,"), "gc_appointment"),
    (("founder", "co-founder"), "founder_departure"),
)
_MILESTONE_KEYWORDS = (
    (("acqui",), "acquisition_announced"),
    # "invest" alone is a substring of "investigation" -- "investor"/
    # "investment" are specific enough not to collide with the regulatory
    # bucket below (a real false positive caught by this module's own tests).
    (("fund", "raise", "investor", "investment"), "funding_announcement"),
    (("litigat", "lawsuit", "suit"), "litigation_filed"),
    (("regulat", "investigation"), "regulatory_action"),
    (("launch", "product"), "product_launch"),
)


def production_signal_category(interaction_type: str | None, meta: dict) -> str | None:
    """Best-effort mapping from a PRODUCTION RelationshipInteraction's real
    interaction_type + meta_json shape onto this module's fine-grained
    category taxonomy. Returns None -- not a guess -- when no honest mapping
    exists: production's `promotion`/`profile_update`/`account_cooling`
    kinds have no fine-grained bucket here, and affinity() already treats a
    None category as neutral 0.5 rather than a fabricated confidence."""
    if interaction_type == "job_change":
        title = (meta.get("new_title") or "").lower()
        if not title:
            return None
        for keywords, category in _JOB_TITLE_KEYWORDS:
            if any(k in title for k in keywords):
                return category
        return "exec_appointment"
    if interaction_type == "new_post":
        milestone = (meta.get("milestone_type") or "").lower()
        if not milestone:
            return None
        for keywords, category in _MILESTONE_KEYWORDS:
            if any(k in milestone for k in keywords):
                return category
        return None
    return None


def affinity(practice_area: str | None, signal_category: str | None) -> float:
    """Look up the (practice_area, signal_category) affinity. Unknown or
    unset inputs return a neutral 0.5 -- fail toward "uninformative", not
    toward a fabricated high or low confidence."""
    if not practice_area or not signal_category:
        return 0.5
    return SIGNAL_AFFINITY_SEED.get(practice_area, {}).get(signal_category, 0.5)


def update_affinity_from_outcomes(engagement_rows: list[dict]) -> dict[str, dict[str, float]]:
    """Recompute affinity from REAL engagement outcomes instead of the seed
    table -- this is the function that makes the taxonomy 'accumulate' the
    way the moat story requires. `engagement_rows`: dicts with
    practice_area, signal_category, engaged (bool). Returns a new affinity
    table; does not mutate SIGNAL_AFFINITY_SEED (the seed stays the
    documented starting prior; a caller decides whether/how to persist the
    recomputed table). Falls back to the seed value for any (area,
    category) pair with no observed rows -- doesn't overwrite prior with
    noise from zero examples."""
    from collections import defaultdict

    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])  # [engaged, total]
    for row in engagement_rows:
        pa, cat, engaged = row.get("practice_area"), row.get("signal_category"), row.get("engaged")
        if not pa or not cat:
            continue
        counts[(pa, cat)][1] += 1
        if engaged:
            counts[(pa, cat)][0] += 1

    out: dict[str, dict[str, float]] = {
        pa: dict(cats) for pa, cats in SIGNAL_AFFINITY_SEED.items()
    }
    for (pa, cat), (engaged, total) in counts.items():
        if total == 0:
            continue
        out.setdefault(pa, {})[cat] = round(engaged / total, 3)
    return out
