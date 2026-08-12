"""backend/observe/harnesses/replay.py : HARNESS #1 -- historical replay, the
foundation harness every other harness in this package leans on for
credibility. "If Surplus had run its current intelligence system at time T,
what would it have known and what would it have recommended?"

Freezes the world at T using ranking_trace.compute_trace(..., as_of=T): every
factor function in ranking_trace.py takes the same as_of cutoff, filtering
interactions/signals to `occurred_at <= T` (see that module's docstring on
compute_trace). This matters most for historical_behavior: BEFORE as_of
support was added, that factor queried a lawyer's ENTIRE draft history with
no time bound, so a naive replay would have "known" the outcome of drafts
written after T -- a leak straight into the one factor meant to represent
accumulated, pre-T experience. tests/test_observe_replay.py's
test_historical_behavior_excludes_interactions_after_as_of is the explicit,
required test for this invariant (Observe spec section 4: "Create an explicit
test for this").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from .. import cohort_query as cq
from ..harness import HarnessResult, now as harness_now
from ...demo import ranking_trace as rt
from ... import models

_UTC = timezone.utc

HARNESS_ID = "historical_replay"
HARNESS_NAME = "Historical Replay Harness"
HARNESS_VERSION = "v1"

# score >= this threshold -> "would have recommended engaging". An
# uncalibrated midpoint on compute_trace's 0-1 weighted-sum score, not a
# tuned decision boundary -- documented so a reader doesn't mistake this for
# a fit probability threshold.
PREDICTION_THRESHOLD = 0.5

# Cap on drafts replayed per lawyer, so run() stays bounded on a large cohort.
MAX_CASES_PER_LAWYER = 50
MAX_SAMPLE_CASES_IN_RESULT = 20


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


def replay_one(db, user, contact, as_of: datetime) -> dict:
    """Reconstruct the trace for `contact` as it would have looked at
    `as_of`, then compare the prediction against what actually happened
    after `as_of`."""
    trace = rt.compute_trace(db, user, contact, as_of=as_of)

    later_rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact.id)
        .order_by(models.RelationshipInteraction.occurred_at.asc())
    ).scalars().all()
    cutoff = _aware(as_of)
    later_rows = [r for r in later_rows if _aware(r.occurred_at) > cutoff]
    observed_sequence = [r.title for r in later_rows]

    outcome_row = next(
        (r for r in later_rows if r.title and r.title.startswith("Drafted follow-up")), None)
    observed_engaged = None
    if outcome_row is not None:
        meta = json.loads(outcome_row.meta_json or "{}")
        observed_engaged = bool(meta.get("engaged"))

    return {
        "contact_id": contact.id, "contact_name": contact.name,
        "as_of": as_of.isoformat(),
        "reconstructed_score": round(trace.opportunity_score, 4),
        "reconstructed_factors": [
            {"name": f.name, "value": round(f.value, 4), "evidence": f.evidence}
            for f in trace.factors
        ],
        "observed_sequence": observed_sequence,
        "observed_engaged": observed_engaged,
    }


def run(db, cohort_id: str) -> HarnessResult:
    """Replay every drafted signal in the cohort at its own occurred_at
    timestamp (T = the moment the signal would have been drafted),
    reconstruct the trace with only pre-T information, and compare the
    reconstruction's implied prediction to what actually happened after."""
    pairs = cq.users_and_contacts(db, cohort_id)
    cases = []
    leakage_violations = 0
    reconstruction_failures = 0
    agreement = []

    for user, contacts in pairs:
        contact_ids = [c.id for c in contacts]
        if not contact_ids:
            continue
        drafts = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id.in_(contact_ids),
                   models.RelationshipInteraction.title.like("Drafted follow-up%"))
        ).scalars().all()
        by_contact = {c.id: c for c in contacts}

        for draft in drafts[:MAX_CASES_PER_LAWYER]:
            contact = by_contact.get(draft.contact_id)
            if contact is None:
                continue
            as_of = _aware(draft.occurred_at)
            try:
                result = replay_one(db, user, contact, as_of)
            except Exception:
                reconstruction_failures += 1
                continue

            # No-future-leakage spot-check on every replayed case (in
            # addition to the dedicated unit test): relationship_strength's
            # interaction count should never exceed the TRUE pre-as_of count.
            true_pre_t_count = sum(
                1 for r in db.execute(
                    select(models.RelationshipInteraction)
                    .where(models.RelationshipInteraction.contact_id == contact.id)
                ).scalars().all()
                if _aware(r.occurred_at) <= as_of
            )
            reported_count = next(
                (f["evidence"].get("total_interactions") for f in result["reconstructed_factors"]
                 if f["name"] == "relationship_strength"), None)
            if reported_count is not None and reported_count > true_pre_t_count:
                leakage_violations += 1

            meta = json.loads(draft.meta_json or "{}")
            predicted_would_engage = result["reconstructed_score"] >= PREDICTION_THRESHOLD
            actual_engaged = bool(meta.get("engaged"))
            agreement.append(predicted_would_engage == actual_engaged)

            cases.append({**result, "predicted_would_engage": predicted_would_engage,
                          "actual_engaged": actual_engaged})

    n = len(cases)
    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=n + reconstruction_failures,
        cases_passed=n, cases_failed=reconstruction_failures,
        metrics={
            "temporal_leakage_violations": leakage_violations,
            "reconstruction_failures": reconstruction_failures,
            "prediction_outcome_agreement_rate": (
                round(sum(agreement) / len(agreement), 4) if agreement else None),
            "prediction_threshold": PREDICTION_THRESHOLD,
            "prediction_threshold_note": "uncalibrated midpoint, not a tuned decision boundary",
            "n_cases_replayed": n,
            "sample_cases": cases[:MAX_SAMPLE_CASES_IN_RESULT],
        },
        failures=[], provenance="demo",
    )
