"""agents/relationship/solicitation_signals.py : resolves REAL signals into a
solicitation.SolicitationContext and applies the Rule 7.3 gate.

backend/solicitation.py is deliberately pure -- no DB reads, no network (see
its own docstring). This module is the impure side: the one place that turns
a User + Contact + channel + real send history into the inputs evaluate()
needs, and the one function callers actually consult before an AUTOMATED
send. Manual UI sends never pass through here, same posture as every other
gate in pipeline/send/sender.py.

Everything here fails toward the RESTRICTIVE outcome when a signal is
missing: no bar_jurisdiction on the user -> unknown-jurisdiction rule
(fail-closed); no relationship_type on the contact -> PROSPECT (the only
category the gate restricts). Nothing here ever infers an exemption from
scraped or enriched data -- exemptions are set explicitly, same posture as
VIP starring.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ... import models
from ...solicitation import RelationshipType, SolicitationContext, Verdict, evaluate

# Matches the widest jurisdiction window configured in solicitation.py today
# (FL's 30-day volume cap / cooldown). Revisit if a future jurisdiction adds
# a longer window -- this is a query bound, not a rule value.
_LOOKBACK_DAYS = 30


def _relationship_type(contact) -> RelationshipType:
    raw = (getattr(contact, "relationship_type", None) or "").strip()
    try:
        return RelationshipType(raw) if raw else RelationshipType.PROSPECT
    except ValueError:
        # Unrecognized value in the column (bad data, or a RelationshipType
        # added after this deploy) -- fail closed to the restricted category
        # rather than silently exempting.
        return RelationshipType.PROSPECT


def _recent_solicitation_count(db, user_id: int, contact_id: Optional[int],
                                *, window_days: int = _LOOKBACK_DAYS) -> int:
    """Count real outbound touches to this contact in the trailing window.
    RelationshipInteraction is the append-only touch log every send path
    already writes to -- this is live send history, not a caller-supplied
    placeholder. A contact with no id yet (mid-capture) counts as zero."""
    if not contact_id:
        return 0
    from sqlalchemy import func, select
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    stmt = (
        select(func.count())
        .select_from(models.RelationshipInteraction)
        .where(
            models.RelationshipInteraction.contact_id == contact_id,
            models.RelationshipInteraction.actor_user_id == user_id,
            models.RelationshipInteraction.direction == "outbound",
            models.RelationshipInteraction.occurred_at >= cutoff,
        )
    )
    return db.execute(stmt).scalar_one()


def resolve_context(db, user, contact, channel: str, *,
                     matter_is_sensitive: bool = False,
                     triggering_event_date: Optional[date] = None) -> SolicitationContext:
    """Build the real SolicitationContext for one automated send. Unset
    bar_jurisdiction resolves to "" -- solicitation.evaluate() treats any
    jurisdiction absent from JURISDICTION_RULES as unknown and fails closed,
    so an empty string needs no special-casing here."""
    jurisdiction = (getattr(user, "bar_jurisdiction", None) or "").strip()
    count = _recent_solicitation_count(db, user.id, getattr(contact, "id", None))
    return SolicitationContext(
        jurisdiction=jurisdiction,
        channel=channel,
        relationship_type=_relationship_type(contact),
        matter_is_sensitive=matter_is_sensitive,
        triggering_event_date=triggering_event_date,
        recent_solicitation_count_in_window=count,
    )


def check(db, user, contact, channel: str, **kwargs) -> Verdict:
    """The one function callers use: resolve real signals, evaluate, return
    the Verdict. Never raises on missing data -- unset fields resolve to the
    fail-closed defaults built into resolve_context/evaluate above."""
    return evaluate(resolve_context(db, user, contact, channel, **kwargs))
