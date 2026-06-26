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
    from ... import models
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
_META_KEYS = {"channel_preference", "register", "avg_response_latency"}
# How a fact key reads as a grounding clause. Unknown keys fall back to "key: value".
_KEY_PHRASES = {
    "based_in": "based in {v}",
    "hometown": "from {v}",
    "school": "went to {v}",
    "interest": "into {v}",
    "works_on": "works on {v}",
    "birthday": "birthday is {v}",
}


def draft_grounding(db, contact_id: int) -> tuple[list[str], list[dict]]:
    """High-confidence store facts as (grounding_lines, provenance) for a draft.

    `grounding_lines` are ready-to-inject clauses the composer appends to its
    asserted grounding (the data layer, not the prompt). `provenance` tags each
    surfaced fact with source + observed_at + mode="graph" so the assembled
    context is LEGIBLE -- we can always see what came from the knowledge store.
    Skips keys already shown elsewhere (company/title). Best-effort: any read
    failure returns empty, never breaks a draft."""
    try:
        rows = get_facts(db, contact_id, high_confidence_only=True)
    except Exception:  # noqa: BLE001 - context read must never break drafting
        return [], []
    lines: list[str] = []
    prov: list[dict] = []
    for r in rows:
        v = (r.value or "").strip()
        if not v or r.key in _SHOWN_ELSEWHERE or r.key in _META_KEYS:
            continue
        phrase = _KEY_PHRASES.get(r.key, "{k}: {v}").format(
            k=r.key.replace("_", " "), v=v)
        lines.append(phrase)
        prov.append({"key": r.key, "value": v, "source": r.source,
                     "observed_at": r.observed_at, "mode": "graph"})
    return lines, prov


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
    from ... import models
    q = db.query(models.ContactFact).filter_by(contact_id=contact_id)
    if key is not None:
        q = q.filter_by(key=key)
    if source is not None:
        q = q.filter_by(source=source)
    rows = q.order_by(models.ContactFact.observed_at.desc()).all()
    if high_confidence_only:
        rows = [r for r in rows if r.confidence == "high"]
    return rows
