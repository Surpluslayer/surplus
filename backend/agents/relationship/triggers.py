"""agents/relationship/triggers.py : the Flow-1 time-trigger engine.

Flow 1 = PROACTIVE messaging. A dated fact in the store (a birthday today, a
flight tomorrow, a work anniversary) is its OWN reason to reach out -- there's no
"should I?" judgment, so it bypasses triage and fires deterministically.

This module is the deterministic half: SCAN the store for facts that have come
due, FIRE each (hand the moment to a callback that composes/stages the message),
then CONSUME the trigger via the fact lifecycle -- one-off facts are deleted, a
recurring fact is advanced to next year. The actual drafting is delegated through
the `on_due` callback, so the engine never touches the composer or the harness.

The history is never lost: consuming a fact only prunes the materialized current-
state view; the event that created it still lives in the timeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .contact_memory import due_facts, mark_fired


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scan_and_fire(
    db,
    *,
    user_id: Optional[int] = None,
    now: Optional[datetime] = None,
    within_days: int = 0,
    on_due: Optional[Callable[[Any, Any], None]] = None,
) -> list[dict]:
    """Fire every due trigger once. For each due fact: resolve its contact, call
    `on_due(fact, contact)` (the consumer composes/stages the proactive draft),
    then consume the trigger via `mark_fired`. `within_days` looks ahead (e.g. 1
    to catch tomorrow's flight today). Returns a record of what fired + how it was
    disposed. An `on_due` failure is isolated so one bad draft can't block the rest;
    the trigger is still consumed so it can't wedge the queue."""
    from ... import models
    now = now or _now()
    fired: list[dict] = []
    for fact in due_facts(db, now=now, user_id=user_id, within_days=within_days):
        contact = db.get(models.Contact, fact.contact_id)
        if contact is None:
            # Orphaned fact (contact gone): consume it and move on.
            mark_fired(db, fact, now, commit=False)
            continue
        key, value = fact.key, fact.value          # capture before a delete
        if on_due is not None:
            try:
                on_due(fact, contact)
            except Exception as exc:  # noqa: BLE001 - one trigger never blocks others
                print(f"  [triggers] on_due failed for {key} "
                      f"(contact {fact.contact_id}): {type(exc).__name__}: {exc}")
        disposition = mark_fired(db, fact, now, commit=False)
        fired.append({"contact_id": contact.id, "key": key, "value": value,
                      "disposition": disposition})
    db.commit()
    return fired
