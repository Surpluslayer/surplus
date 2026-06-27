"""agents/relationship/observability.py : a read-only health snapshot of the
deterministic relationship layer -- what the system KNOWS and what it'll act on.

Built to make harness development legible: one call shows fact-store coverage, the
proactive due queue, the automation-flag state, and the scheduler heartbeats. So you
can answer "what context does the agent actually have?" without spelunking the DB.
Pure reads; owner-scoped for per-user data, plus the global flag/scheduler state.
"""
from __future__ import annotations

from collections import Counter

from ... import models
from . import proactive
from .sender import _automated_channels, _automation_master_on
from .updates_scheduler import last_tick as _updates_last_tick


def _fact_stats(db, user_id: int) -> dict:
    """Fact-store coverage for one user: how much typed memory exists and where it
    came from. The signal for 'is the context layer actually populated?'."""
    facts = (db.query(models.ContactFact)
             .filter(models.ContactFact.user_id == user_id).all())
    contacts_with = len({f.contact_id for f in facts})
    total_contacts = (db.query(models.Contact)
                      .filter(models.Contact.user_id == user_id).count())
    return {
        "total_facts": len(facts),
        "contacts_with_facts": contacts_with,
        "total_contacts": total_contacts,
        "coverage_pct": (round(100 * contacts_with / total_contacts, 1)
                         if total_contacts else 0.0),
        "by_key": dict(Counter(f.key for f in facts).most_common()),
        "by_source": dict(Counter((f.source or "?") for f in facts)),
        "by_confidence": dict(Counter((f.confidence or "?") for f in facts)),
    }


def relationship_status(db, user_id: int) -> dict:
    """One-shot health snapshot of the deterministic layer for a user."""
    due = proactive.collect_due(db, user_id)
    chans = _automated_channels()
    return {
        "facts": _fact_stats(db, user_id),
        "due": {
            "contacts": due["counts"]["contacts"],
            "triggers": due["counts"]["triggers"],
        },
        "automation": {
            # The agent only auto-sends when master is on AND the channel is allowed.
            "master_on": _automation_master_on(),
            "channels": sorted(chans) if chans is not None else "all",
        },
        "schedulers": {
            # In-process tick state (per-worker; empty on a worker that hasn't ticked).
            "updates": _updates_last_tick(),
            "proactive": proactive.last_tick(),
        },
    }
