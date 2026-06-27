"""agents/relationship/contact_memory.py : the per-contact MEMORY store.

A thin read/write API over the ``ContactFact`` table (see models.py). This is
the one place any source (LinkedIn, WhatsApp, calendar, email, manual,
enrichment) writes durable typed facts about a person, and the one place a
reader pulls them back. Deliberately minimal for now: upsert + read. The
time-trigger engine and per-source ingestion workers build ON TOP of this later
-- the schema already carries the `due_date`/`recurring` hooks they'll use.

Upsert is keyed on (contact_id, key, dedup_key) so a source re-observing the same
fact updates it in place instead of stacking duplicates. A contact can still hold
several facts of the same `key` by varying `dedup_key` (interest:climbing,
interest:jazz).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """Normalize DB datetimes (often naive UTC from SQLite) for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def upsert_fact(
    db,
    user_id: int,
    contact_id: int,
    key: str,
    value: str = "",
    *,
    source: str = "manual",
    confidence: str = "high",
    due_date: Optional[datetime] = None,
    recurring: bool = False,
    dedup_key: str = "",
    commit: bool = True,
) -> Any:
    """Insert or update one fact. Keyed on (contact_id, key, dedup_key): a repeat
    observation refreshes the value/source/confidence + `observed_at` in place.
    Returns the row."""
    from .... import models
    row = (db.query(models.ContactFact)
             .filter_by(contact_id=contact_id, key=key, dedup_key=dedup_key)
             .one_or_none())
    if row is None:
        row = models.ContactFact(user_id=user_id, contact_id=contact_id,
                                 key=key, dedup_key=dedup_key)
        db.add(row)
    row.value = value or ""
    row.source = source
    row.confidence = confidence
    row.due_date = due_date
    row.recurring = recurring
    row.observed_at = _now()
    if commit:
        db.commit()
    return row


# Keys already represented elsewhere in the draft context (company/title ride the
# who-line "Name, Title at Company"), so surfacing them again from the store would
# double up. The store still HOLDS them (for provenance + future readers); we just
# don't re-ground them.
_SHOWN_ELSEWHERE = {"company", "title", "role", "headline"}
# META facts inform HOW / WHERE to reach someone (channel preference, register),
# not WHAT to say -- so they're stored + readable but never grounded into a draft.
_META_KEYS = {"channel_preference", "register", "avg_response_latency",
              "thread_summary"}
# How a fact key reads as a grounding clause. Unknown keys fall back to "key: value".
_KEY_PHRASES = {
    "based_in": "based in {v}",
    "hometown": "from {v}",
    "school": "went to {v}",
    "interest": "into {v}",
    "works_on": "works on {v}",
    "about": "what they work on: {v}",
    "birthday": "birthday is {v}",
}


def draft_grounding(db, contact_id: int) -> tuple[list[str], list[str], list[dict]]:
    """Store facts ready for a draft as (asserted, optional, provenance).

    Confidence-gated like the rest of the SELECT stage: HIGH-confidence attribute
    facts -> `asserted` (the draft may state them); LOW-confidence -> `optional`
    (color it may use, never required -> anti-fabrication stays structural). META
    facts (channel_preference/register) and keys already shown elsewhere
    (company/title) are excluded from both. `provenance` tags every surfaced fact
    with source + observed_at + mode="graph" for legibility. Best-effort: any read
    failure returns empties, never breaks a draft."""
    try:
        rows = get_facts(db, contact_id)
    except Exception:  # noqa: BLE001 - context read must never break drafting
        return [], [], []
    asserted: list[str] = []
    optional: list[str] = []
    prov: list[dict] = []
    for r in rows:
        v = (r.value or "").strip()
        if not v or r.key in _SHOWN_ELSEWHERE or r.key in _META_KEYS:
            continue
        phrase = _KEY_PHRASES.get(r.key, "{k}: {v}").format(
            k=r.key.replace("_", " "), v=v[:240])
        (asserted if r.confidence == "high" else optional).append(phrase)
        prov.append({"key": r.key, "value": v, "source": r.source,
                     "confidence": r.confidence,
                     "observed_at": r.observed_at, "mode": "graph"})
    return asserted, optional, prov


def get_facts(
    db,
    contact_id: int,
    *,
    key: Optional[str] = None,
    source: Optional[str] = None,
    high_confidence_only: bool = False,
) -> list:
    """Read a contact's facts, newest-observed first. Optional filters by `key`
    or `source`; `high_confidence_only` drops low-confidence color (mirrors the
    drafting SELECT stage's confidence gate)."""
    from .... import models
    q = db.query(models.ContactFact).filter_by(contact_id=contact_id)
    if key is not None:
        q = q.filter_by(key=key)
    if source is not None:
        q = q.filter_by(source=source)
    rows = q.order_by(models.ContactFact.observed_at.desc()).all()
    if high_confidence_only:
        rows = [r for r in rows if r.confidence == "high"]
    return rows


def delete_fact(db, contact_id: int, key: str, dedup_key: str = "",
                *, commit: bool = True) -> bool:
    """Remove a fact (a correction, a cross-key clear, or a one-off trigger that's
    been consumed). Returns True if a row was deleted. History is never lost --
    the event that created the fact still lives in the timeline."""
    from .... import models
    row = (db.query(models.ContactFact)
             .filter_by(contact_id=contact_id, key=key, dedup_key=dedup_key)
             .one_or_none())
    if row is None:
        return False
    db.delete(row)
    if commit:
        db.commit()
    return True


def due_facts(db, *, now, user_id: Optional[int] = None, within_days: int = 0,
              stale_after_days: int = 30) -> list:
    """The dated facts whose time-trigger has come due: `due_date` <= now (+
    `within_days` lookahead), and not already fired for THIS occurrence. Recurring
    facts store their NEXT occurrence in `due_date`, so the same query serves both
    -- the per-occurrence guard is `last_fired_at`.

    A one-off whose `due_date` is more than `stale_after_days` in the past is NOT
    returned: the moment has long gone, so it's neither worth firing (a belated
    congrats reads worse than silence) nor worth showing in the feed. Recurring
    facts auto-advance, so they're never stale. `stale_after_days<=0` disables the
    floor (return everything past-due)."""
    from datetime import timedelta
    from .... import models
    now = _aware(now)
    horizon = now + timedelta(days=within_days)
    floor = (now - timedelta(days=stale_after_days)) if stale_after_days > 0 else None
    q = db.query(models.ContactFact).filter(
        models.ContactFact.due_date.isnot(None),
        models.ContactFact.due_date <= horizon)
    if user_id is not None:
        q = q.filter(models.ContactFact.user_id == user_id)
    out = []
    for r in q.all():
        due = _aware(r.due_date)
        fired = _aware(r.last_fired_at) if r.last_fired_at is not None else None
        if fired is not None and fired >= due:
            continue  # already fired this occurrence
        if floor is not None and not r.recurring and due < floor:
            continue  # ancient one-off -> stale, drop from the feed/fire set
        out.append(r)
    return out


def expire_stale(db, *, now=None, user_id: Optional[int] = None,
                 grace_days: int = 30, commit: bool = True) -> int:
    """Hard-delete ANCIENT one-off triggers that nobody consumed: non-recurring,
    `due_date` older than `grace_days`, never fired. In collect-only mode nothing
    calls `mark_fired`, so a flight from last month would otherwise linger as a
    zombie fact forever -- this is the GC for the materialized store. Recurring
    facts auto-advance and are left alone. Returns the delete count."""
    from datetime import datetime, timezone, timedelta
    from .... import models
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=max(grace_days, 0))
    q = db.query(models.ContactFact).filter(
        models.ContactFact.due_date.isnot(None),
        models.ContactFact.due_date < floor,
        models.ContactFact.recurring.is_(False),
        models.ContactFact.last_fired_at.is_(None))
    if user_id is not None:
        q = q.filter(models.ContactFact.user_id == user_id)
    rows = q.all()
    for r in rows:
        db.delete(r)
    if commit:
        db.commit()
    return len(rows)


def mark_fired(db, fact, now, *, commit: bool = True) -> str:
    """Consume a fired trigger. Recurring (birthday) -> stamp `last_fired_at` and
    advance `due_date` to the next occurrence (so it never re-fires this year and
    fires again next). One-off (a flight) -> DELETE it (the moment is past). Returns
    'advanced' or 'deleted'."""
    from datetime import timedelta
    if fact.recurring:
        fact.last_fired_at = now
        fact.due_date = fact.due_date + timedelta(days=365)
        if commit:
            db.commit()
        return "advanced"
    db.delete(fact)
    if commit:
        db.commit()
    return "deleted"
