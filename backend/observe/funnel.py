"""backend/observe/funnel.py : durable stage-passage log for the
detection->outreach funnel ("SURPLUS ACTIVITY — LAST 30 DAYS": signals
detected -> matched practice -> matched signal libraries -> relationship
context -> high-confidence -> sent), backed by models.FunnelEvent.

LOGGING ONLY. This module writes rows; it does not expose an API route or
render anything -- that is deliberately left for a future PR once the
product decides how the funnel should look. What's here is meant to make
that PR additive (read a table) instead of archaeological (reconstruct
history that was never recorded).

Six stages, not the full funnel from the product mockup this was scoped
against -- "events processed" (raw pre-filter ingestion volume) and
"drafted" (a draft was shown, independent of whether it was ever sent)
aren't recorded here because neither is durable ANYWHERE in the pipeline
today (see this module's PR description); "saved" isn't recorded because
there's no product action that means "save an opportunity" to record.
Logging those needs its own instrumentation decision first, not a
retrofit onto this table.

Every recording function is best-effort and MUST NOT raise: this is
observability bookkeeping riding alongside real signal-detection and
real-send code paths, and a funnel-logging bug must never drop a signal,
a draft, or a send. Callers wrap accordingly (see relationship_watch._emit
and pipeline/send/sender.send_and_log for the two call sites).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from .. import models

_UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(_UTC)


# ── the stage vocabulary (models.FunnelEvent.stage's closed-in-practice set) ─
SIGNAL_DETECTED = "signal_detected"
MATCHED_PRACTICE = "matched_practice"
MATCHED_SIGNAL_LIBRARY = "matched_signal_library"
RELATIONSHIP_CONTEXT = "relationship_context"
HIGH_CONFIDENCE = "high_confidence"
SENT = "sent"

STAGES = (SIGNAL_DETECTED, MATCHED_PRACTICE, MATCHED_SIGNAL_LIBRARY,
          RELATIONSHIP_CONTEXT, HIGH_CONFIDENCE, SENT)

# opportunity_score cutoff for "high confidence". FACTOR_WEIGHTS (ranking_trace.py)
# sums to 1.0, so scores land in a comparable [0, ~1] range, but this exact
# number is a starting point for product/ops to tune, NOT a calibrated
# threshold -- same "architecture, not a certified value" posture as
# solicitation.py's JURISDICTION_RULES. Revisit once real opportunity_score
# distributions exist to calibrate against.
HIGH_CONFIDENCE_THRESHOLD = 0.65


def _record(db, *, user_id: int, stage: str, contact_id: int | None = None,
            interaction_id: int | None = None, value: float | None = None,
            occurred_at: datetime | None = None) -> None:
    db.add(models.FunnelEvent(
        user_id=user_id, contact_id=contact_id, interaction_id=interaction_id,
        stage=stage, value=value, occurred_at=occurred_at or _now(),
    ))


def record_signal_funnel(db, contact: models.Contact,
                         interaction: models.RelationshipInteraction) -> None:
    """Log signal_detected plus every downstream stage this ONE freshly
    emitted signal reaches, scored against its contact's owner right now.

    Called once, at detection time (relationship_watch._emit, right after
    the interaction is flushed) -- not recomputed later, on purpose: these
    scores read the owner's CURRENT practice_area and the book's CURRENT
    relationship state, so evaluating them at any other time would silently
    answer "would this match today", not "did this match when it happened".

    Never raises -- wrapped so a scoring bug can never drop the signal
    itself or the auto-draft it triggers."""
    try:
        user = db.get(models.User, contact.user_id)
        if user is None:
            return
        occurred_at = interaction.occurred_at or _now()
        _record(db, user_id=user.id, contact_id=contact.id,
                interaction_id=interaction.id, stage=SIGNAL_DETECTED,
                occurred_at=occurred_at)

        from ..demo import signal_taxonomy as tax
        meta = json.loads(interaction.meta_json or "{}")
        category = meta.get("signal_category") or tax.production_signal_category(
            interaction.interaction_type, meta)
        affinity = tax.affinity(tax.effective_practice_area(user), category)
        # affinity() returns exactly 0.5 (neutral) whenever category is None, so
        # this also implies category is real -- no separate "and category" guard.
        if affinity > 0.5:
            _record(db, user_id=user.id, contact_id=contact.id,
                    interaction_id=interaction.id, stage=MATCHED_PRACTICE,
                    value=affinity, occurred_at=occurred_at)
        if category is not None:
            _record(db, user_id=user.id, contact_id=contact.id,
                    interaction_id=interaction.id, stage=MATCHED_SIGNAL_LIBRARY,
                    occurred_at=occurred_at)

        # Same evidence backend/observe/adapters.relationship_trace shows in the
        # panel -- reused, not re-derived (this package's own DRY rule). as_of
        # excludes THIS signal from its own relationship-duration evidence --
        # see _relationship_context's own docstring for why that matters here.
        from datetime import timedelta
        from .adapters import _relationship_context
        ctx = _relationship_context(db, user, contact,
                                    as_of=occurred_at - timedelta(microseconds=1))
        has_context = (ctx.get("relationship_duration_days") is not None
                       or (ctx.get("shared_entities") or 0) > 0)
        if has_context:
            _record(db, user_id=user.id, contact_id=contact.id,
                    interaction_id=interaction.id, stage=RELATIONSHIP_CONTEXT,
                    occurred_at=occurred_at)

        from ..demo import ranking_trace as rt
        score = rt.compute_trace(db, user, contact).opportunity_score
        if score >= HIGH_CONFIDENCE_THRESHOLD:
            _record(db, user_id=user.id, contact_id=contact.id,
                    interaction_id=interaction.id, stage=HIGH_CONFIDENCE,
                    value=score, occurred_at=occurred_at)
    except Exception as exc:  # noqa: BLE001 : logging must never break detection
        print(f"  [funnel] signal funnel skipped for contact={contact.id}: "
              f"{type(exc).__name__}: {exc}", flush=True)


def record_sent(db, *, user_id: int, contact_id: int, channel: str) -> None:
    """Log a real outbound send. Scoped to channel == 'linkedin' only, since
    what was asked for is "sent DMs" specifically -- a LinkedIn DM, not
    outbound email (send_followup_email is a separate path this function
    is never called from). Never raises."""
    if channel != "linkedin":
        return
    try:
        _record(db, user_id=user_id, contact_id=contact_id, stage=SENT)
    except Exception as exc:  # noqa: BLE001 : logging must never break a real send
        print(f"  [funnel] sent logging skipped for contact={contact_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)


def rollup(db, user_id: int, *, since: datetime) -> dict:
    """stage -> count for one user's book since `since`. The one aggregation
    query a future funnel-UI PR needs; every stage in STAGES is present
    (zero, not missing) even when nothing landed there, so a caller can
    render a bar chart without a defaultdict of its own."""
    rows = db.execute(
        select(models.FunnelEvent.stage, func.count())
        .where(models.FunnelEvent.user_id == user_id,
               models.FunnelEvent.occurred_at >= since)
        .group_by(models.FunnelEvent.stage)
    ).all()
    counts = {stage: 0 for stage in STAGES}
    counts.update({stage: n for stage, n in rows if stage in counts})
    return counts
