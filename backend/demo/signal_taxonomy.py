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

from datetime import datetime, timezone

PRACTICE_AREAS = (
    "corporate_ma", "litigation", "real_estate", "employment_labor",
    "ip", "regulatory",
)

# Same posture as solicitation.py's _DEFAULT_JURISDICTION: this is a
# single-tenant demo/observability tool for one account whose real practice
# is startup law, which predates any startup-law-specific bucket in this
# taxonomy's 6 categories. corporate_ma is the closest existing one --
# formation, financing, equity, and exits are its core content -- so an
# unset practice_area defaults there via effective_practice_area() below
# instead of the uninformative neutral 0.5. Revisit before this serves an
# account whose real practice isn't startup-adjacent corporate work.
_DEFAULT_PRACTICE_AREA = "corporate_ma"


def effective_practice_area(user) -> str:
    """The practice_area affinity/ranking should actually use for `user`:
    their real value if set, else `_DEFAULT_PRACTICE_AREA`. Centralizes the
    default so every consumer (ranking, funnel, outcomes, Observe's trace)
    agrees on it instead of each falling back to neutral 0.5 independently."""
    return (getattr(user, "practice_area", None) or "").strip() or _DEFAULT_PRACTICE_AREA

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


# Production's ACTUAL milestone vocabulary, from updates_engine._POST_SYSTEM:
#   "type":"raise|launch|role|award|milestone"
# Only two of those five ever matched the keyword table below ("raise" ->
# funding, "launch" -> product), so `role`, `award` and `milestone` fell
# through to None and every such signal logged "unclassified" -- which pins
# practice_fit at a neutral 0.5 and leaves historical_behavior with "no signal
# category to compare against". `role` has an honest home here. `award` and
# `milestone` genuinely do not: this taxonomy has eight categories and none of
# them means "generic good news", so they stay None rather than being forced
# into the nearest-looking bucket.
_PRODUCTION_MILESTONE_TYPES = {"raise", "launch", "role", "award", "milestone"}
_NO_HONEST_BUCKET = {"award", "milestone"}


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
        if milestone == "role":
            # A role announced in a post is the same event a job_change row
            # describes, so reuse that reading rather than inventing a
            # parallel one -- headline/summary carry the title text here.
            text = f"{meta.get('headline') or ''} {meta.get('summary') or ''}".lower()
            for keywords, category in _JOB_TITLE_KEYWORDS:
                if any(k in text for k in keywords):
                    return category
            return "exec_appointment"
        return None
    return None


def unmapped_reason(interaction_type: str | None, meta: dict) -> str:
    """Why production_signal_category() returned None, in terms an engineer can
    act on. "no mapping for this interaction_type/milestone_type" without the
    VALUES is not actionable -- it took reading _POST_SYSTEM to discover that
    three of production's five milestone types had no bucket at all."""
    if interaction_type == "job_change":
        return ("job_change with no new_title on meta_json -- nothing to read a "
                "seniority bucket from")
    if interaction_type == "new_post":
        milestone = (meta.get("milestone_type") or "").lower()
        if not milestone:
            return "new_post with no milestone_type on meta_json"
        if milestone in _NO_HONEST_BUCKET:
            return (f"milestone_type={milestone!r} -- a real production value with no "
                    f"honest bucket among this taxonomy's {len(ALL_SIGNAL_CATEGORIES)} "
                    f"categories (none of them means 'generic good news'), so it is "
                    f"left unclassified rather than forced into the nearest one")
        return (f"milestone_type={milestone!r} is outside production's known set "
                f"{sorted(_PRODUCTION_MILESTONE_TYPES)}")
    return (f"interaction_type={interaction_type!r} has no fine-grained category "
            f"in this taxonomy (production also emits promotion / profile_update / "
            f"account_cooling, none of which map)")


# How much observed evidence it takes to move a seed value meaningfully.
#
# The blend is (engaged + prior*K) / (total + K), i.e. the seed acts as K
# pseudo-observations. This matters more than it looks: the original
# recompute overwrote a prior outright on ANY non-zero sample, so a single
# discarded draft drove an affinity to 0.000 and a single send drove it to
# 1.000. That is not learning, it is overfitting to n=1 and calling the
# result a measurement. At K=20 one observation moves a 0.90 prior by about
# 0.04, and a hundred consistent observations largely take over.
PRIOR_STRENGTH = 20


def seed_affinity(practice_area: str | None, signal_category: str | None) -> float:
    """The documented starting prior, ignoring anything learned."""
    if not practice_area or not signal_category:
        return 0.5
    return SIGNAL_AFFINITY_SEED.get(practice_area, {}).get(signal_category, 0.5)


def blended_affinity(practice_area: str | None, signal_category: str | None,
                     engaged: int, total: int) -> float:
    """Seed prior blended with observed outcomes. See PRIOR_STRENGTH."""
    prior = seed_affinity(practice_area, signal_category)
    if total <= 0:
        return prior
    return round((engaged + prior * PRIOR_STRENGTH) / (total + PRIOR_STRENGTH), 4)


def affinity(practice_area: str | None, signal_category: str | None,
             learned: dict | None = None) -> float:
    """Look up the (practice_area, signal_category) affinity. Unknown or
    unset inputs return a neutral 0.5 -- fail toward "uninformative", not
    toward a fabricated high or low confidence.

    `learned`: {(practice_area, signal_category): (engaged, total)} from
    load_learned(), which is how this stops being a fixed seed table. Omitted,
    the seed prior is returned exactly as before -- callers that have no
    session (and every existing test) are unaffected.
    """
    if not practice_area or not signal_category:
        return 0.5
    if learned:
        counts = learned.get((practice_area, signal_category))
        if counts:
            return blended_affinity(practice_area, signal_category, *counts)
    return seed_affinity(practice_area, signal_category)


def load_learned(db) -> dict:
    """{(practice_area, signal_category): (engaged, total)} from the DB.

    Returns {} rather than raising when the table has not been created yet,
    so a database mid-migration falls back to the seed instead of breaking
    every ranking.
    """
    try:
        from sqlalchemy import select
        from .. import models
        rows = db.execute(select(models.SignalAffinity)).scalars().all()
        return {(r.practice_area, r.signal_category): (r.engaged, r.total) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def apply_outcome(db, practice_area: str | None, signal_category: str | None,
                  engaged: bool, *, commit: bool = True) -> bool:
    """Fold ONE freshly recorded outcome into the learned counts, immediately.

    The daily affinity_refresh sweep recomputes the whole table from scratch,
    which is correct but means a lawyer's decision does not show up in the
    affinity numbers until the next sweep. That delay is invisible to the
    product but it is exactly what someone watching the Observe log wants to
    see, and "turn the poll interval down" is the wrong fix -- it makes every
    account pay for a full recompute far more often to shorten one person's
    feedback loop.

    So the write path applies its own delta and the sweep stays the
    reconciler: incremental for latency, periodic recompute for correctness.
    Safe to do incrementally only because record_draft_outcome() is
    idempotent -- a draft that already carries a disposition is never
    re-marked, so an outcome cannot be counted twice.

    Returns True when a row was updated.
    """
    if not practice_area or not signal_category:
        return False
    from sqlalchemy import select
    from .. import models

    row = db.execute(select(models.SignalAffinity).where(
        models.SignalAffinity.practice_area == practice_area,
        models.SignalAffinity.signal_category == signal_category)).scalar_one_or_none()
    if row is None:
        row = models.SignalAffinity(practice_area=practice_area,
                                    signal_category=signal_category,
                                    engaged=0, total=0)
        db.add(row)
    row.total = (row.total or 0) + 1
    if engaged:
        row.engaged = (row.engaged or 0) + 1
    row.updated_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    return True


def refresh_from_outcomes(db) -> dict:
    """Recompute the learned counts from REAL recorded outcomes and persist.

    Reads every drafted follow-up that carries a disposition (written by
    agents/relationship/outcomes.py when the lawyer actually sends or
    snoozes), pairs it with the drafting lawyer's practice_area and the
    signal's category, and stores engaged/total per pair.

    GENERATED ROWS ARE EXCLUDED. backend/demo/cohort.py writes dispositions
    too, and letting those into this table would mean synthetic outcomes
    quietly shaping real lawyers' rankings -- the exact thing the provenance
    layer exists to prevent. Only rows with no generated-provenance tag
    count; prov.OBSERVED rows are real data and do count.

    Returns {"pairs": n, "outcomes": n, "skipped_generated": n}.
    """
    import json as _json
    from sqlalchemy import select
    from .. import models
    from . import provenance as prov

    generated = {
        row_id for (row_id,) in db.execute(
            select(models.DemoProvenance.row_id).where(
                models.DemoProvenance.table_name == "relationship_interactions",
                models.DemoProvenance.data_provenance.in_(tuple(prov.GENERATED_VALUES)))
        ).all()
    }

    practice_by_user = dict(db.execute(
        select(models.User.id, models.User.practice_area)).all())

    counts: dict = {}
    outcomes = skipped = 0
    from ..agents.relationship import outcomes as _outcomes
    rows = db.execute(
        select(models.RelationshipInteraction).where(_outcomes.drafted_filter())
    ).scalars().all()
    for r in rows:
        if not _outcomes.is_drafted(r):
            continue
        if r.id in generated:
            skipped += 1
            continue
        try:
            meta = _json.loads(r.meta_json or "{}") or {}
        except ValueError:
            continue
        disposition = meta.get("disposition")
        category = meta.get("signal_category")
        area = practice_by_user.get(r.actor_user_id)
        if not disposition or not category or not area:
            continue
        outcomes += 1
        engaged, total = counts.get((area, category), (0, 0))
        counts[(area, category)] = (engaged + (1 if meta.get("engaged") else 0), total + 1)

    now = datetime.now(timezone.utc)
    existing = {(r.practice_area, r.signal_category): r
                for r in db.execute(select(models.SignalAffinity)).scalars().all()}
    for (area, category), (engaged, total) in counts.items():
        row = existing.get((area, category))
        if row is None:
            row = models.SignalAffinity(practice_area=area, signal_category=category)
            db.add(row)
        row.engaged, row.total = engaged, total
        row.updated_at = now

    # Drop pairs the recompute no longer supports. Without this the sweep can
    # only ever ADD, which makes it an accumulator rather than a reconciler --
    # so a count inflated by the incremental write path (a row later tagged as
    # generated, an outcome whose interaction was deleted) would persist
    # forever and quietly keep shaping rankings. The recompute is the source
    # of truth, and that has to include being able to correct downward.
    stale = 0
    for key, row in existing.items():
        if key not in counts:
            db.delete(row)
            stale += 1

    db.commit()
    return {"pairs": len(counts), "outcomes": outcomes,
            "skipped_generated": skipped, "stale_pairs_dropped": stale}


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
        # Blended, not replaced. This used to be `engaged / total`, which let
        # a single observation drive an affinity to 0.0 or 1.0 -- overfitting
        # to n=1 and presenting it as a measurement. See PRIOR_STRENGTH.
        out.setdefault(pa, {})[cat] = blended_affinity(pa, cat, engaged, total)
    return out
