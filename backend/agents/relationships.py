"""
agents/relationships.py : the event-native relationship layer (read model).

Surplus is event-native relationship intelligence, not a generic CRM. Every
event creates relationship data; every relationship warms or cools over time.
This module answers, for one person you met: *who they are, what happened, and
what to do next* — assembled purely from data we already persist.

Milestone 1 is intentionally schema-free. `build_timeline` and
`relationship_summary` read only existing columns / rows:

    Prospect (capture metadata)  -> in_person_capture / manual_note / next_step
    OutreachLog                  -> linkedin_outreach (one item per transition)
    Conversion                   -> conversion (ROI outcome)

Later milestones union stored RelationshipInteraction rows (manual notes,
email, calendar) into the same timeline shape without changing this contract.

Everything here is a pure function of the ORM objects passed in : no DB writes,
no network, no provider calls. Inputs are read defensively via getattr so the
functions also work against lightweight stand-ins in tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# A capture/contact with no touch in this many days is "stale" : a deterministic
# nudge, NOT a score. Tunable; deliberately conservative so we don't cry wolf.
STALE_AFTER_DAYS = 14

# Outreach states (canonical, see OutreachLog docstring) that mean THEY replied
# to us : an inbound signal. Everything else we log is something WE did.
_INBOUND_OUTREACH_STATES = {"message_replied", "replied"}

# Stable tiebreak when two timeline items share a timestamp (e.g. the capture
# row and the note both stamped at captured_at). Lower sorts earlier.
_SOURCE_RANK = {
    "in_person_capture": 0,
    "manual_note": 1,
    "next_step": 2,
    "linkedin_outreach": 3,
    "email_interaction": 4,
    "calendar_meeting": 5,
    "relationship_interaction": 6,
    "draft_generated": 7,
    "conversion": 8,
}

# Sorts timeless items (no occurred_at, e.g. Conversion has no timestamp column)
# to the end of the chronological timeline rather than the beginning.
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize to tz-aware UTC. Naive datetimes are common in this codebase
    (SQLite round-trips drop tzinfo); treat them as UTC so comparisons and
    sorting never raise on mixed naive/aware values."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _clean(val: Any) -> Optional[str]:
    """Trimmed non-empty string, else None."""
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _item(source_type, interaction_type, occurred_at, title, summary,
          channel, direction, **metadata) -> dict:
    return {
        "source_type": source_type,
        "interaction_type": interaction_type,
        "occurred_at": _as_aware(occurred_at),
        "title": title,
        "summary": summary,
        "channel": channel,
        "direction": direction,
        "metadata": metadata,
    }


def _event_title(event: Any) -> Optional[str]:
    if event is None:
        return None
    name = _clean(getattr(event, "event_name", None))
    label = _clean(getattr(event, "label", None))
    if name or label:
        return name or label
    eid = getattr(event, "id", None)
    return f"event #{eid}" if eid is not None else None


def build_timeline(prospect: Any) -> list[dict]:
    """Assemble the chronological (oldest-first) relationship timeline for one
    Prospect from existing persisted data. Pure read; never raises on missing
    optional fields."""
    items: list[dict] = []

    event = getattr(prospect, "event", None)
    event_title = _event_title(event)
    captured_at = getattr(prospect, "captured_at", None)

    # ── capture itself ────────────────────────────────────────────────
    source = _clean(getattr(prospect, "source", None))
    if captured_at is not None or source is not None:
        where = f" at {event_title}" if event_title else ""
        items.append(_item(
            "in_person_capture", "captured", captured_at,
            title=f"Captured{where}",
            summary=f"Met {_clean(getattr(prospect, 'name', None)) or 'this person'}"
                    f"{where}." + (f" (via {source})" if source else ""),
            channel="in_person", direction="none",
            event_id=getattr(event, "id", None), event_title=event_title,
            source=source,
        ))

    # ── notes (fun-fact note is shareable; private_note is operator-only) ──
    note = _clean(getattr(prospect, "note", None))
    if note:
        items.append(_item(
            "manual_note", "note", captured_at,
            title="Note", summary=note,
            channel="manual", direction="none", private=False,
        ))
    private_note = _clean(getattr(prospect, "private_note", None))
    if private_note:
        items.append(_item(
            "manual_note", "private_note", captured_at,
            title="Private note", summary=private_note,
            channel="manual", direction="none", private=True,
        ))

    # ── planned follow-up ─────────────────────────────────────────────
    next_step = _clean(getattr(prospect, "next_step", None))
    if next_step:
        items.append(_item(
            "next_step", "next_step", captured_at,
            title="Next step", summary=next_step,
            channel="manual", direction="none",
        ))

    # ── LinkedIn outreach : one item per logged state transition ───────
    for log in (getattr(prospect, "outreach", None) or []):
        state = _clean(getattr(log, "state", None)) or "unknown"
        direction = "inbound" if state in _INBOUND_OUTREACH_STATES else "outbound"
        body = _clean(getattr(log, "body", None))
        items.append(_item(
            "linkedin_outreach", state, getattr(log, "ts", None),
            title=state.replace("_", " ").title(),
            summary=body or state.replace("_", " "),
            channel=_clean(getattr(log, "channel", None)) or "linkedin",
            direction=direction,
            provider=_clean(getattr(log, "provider", None)),
            provider_lead_id=_clean(getattr(log, "provider_lead_id", None)),
        ))

    # ── conversion (ROI outcome) : no timestamp column, sorts to the end ──
    conv = getattr(prospect, "conversion", None)
    if conv is not None:
        state = _clean(getattr(conv, "state", None)) or "unknown"
        label = _clean(getattr(conv, "label", None))
        detail = _clean(getattr(conv, "detail", None))
        items.append(_item(
            "conversion", state, None,
            title=f"Conversion: {state}",
            summary=" — ".join(p for p in (label, detail) if p) or state,
            channel="roi", direction="none",
            goal=_clean(getattr(conv, "goal", None)),
            tier=_clean(getattr(conv, "tier", None)),
            value=getattr(conv, "value", None),
        ))

    items.sort(key=lambda it: (
        it["occurred_at"] or _FAR_FUTURE,
        _SOURCE_RANK.get(it["source_type"], 99),
    ))
    return items


def _latest_outreach(prospect: Any):
    logs = list(getattr(prospect, "outreach", None) or [])
    if not logs:
        return None
    return max(logs, key=lambda o: _as_aware(getattr(o, "ts", None)) or _FAR_FUTURE)


# Placeholder column defaults that mean "nothing was enriched" : surfacing them
# as real signal would be noise, so we treat them as empty.
_ENRICHMENT_PLACEHOLDERS = {"general", "unknown", ""}


def _enriched(val: Any) -> Optional[str]:
    """Like _clean, but also drops the schema-default placeholders so an
    un-enriched row reads as None rather than 'general'."""
    s = _clean(val)
    if s is None or s.lower() in _ENRICHMENT_PLACEHOLDERS:
        return None
    return s


def _identity(prospect: Any) -> dict:
    """Who this person IS, assembled from the scan / LinkedIn enrichment that
    already lives on the Prospect row (headline, bio, what they work on, recent
    activity). Every field is optional : a bare capture yields a sparse dict, not
    a crash. The relationship layer consumes this; it never re-fetches it."""
    return {
        "name": _clean(getattr(prospect, "name", None)),
        "role": _clean(getattr(prospect, "role", None)),
        "company": _clean(getattr(prospect, "company", None)),
        "headline": _enriched(getattr(prospect, "headline", None)),
        "works_on": _enriched(getattr(prospect, "works_on", None)),
        "bio": _enriched(getattr(prospect, "bio", None)),
        "recent_activity": _enriched(getattr(prospect, "recent_activity", None)),
    }


def _how_we_met(prospect: Any) -> dict:
    """The meeting context : where, when, how we captured them, and what was
    actually talked about (the public note). This is the 'how we met' header the
    timeline opens with — distinct from the chronological capture *item*."""
    event = getattr(prospect, "event", None)
    return {
        "event_id": getattr(event, "id", None),
        "event_title": _event_title(event),
        "event_city": _clean(getattr(event, "city", None)),
        "captured_at": _as_aware(getattr(prospect, "captured_at", None)),
        "via": _clean(getattr(prospect, "source", None)),   # scan | link | text
        "context": _clean(getattr(prospect, "note", None)),  # the fun-fact note
    }


def relationship_summary(prospect: Any) -> dict:
    """Deterministic, ML-free snapshot of where this relationship stands.

    Stage precedence (strongest signal wins):
        converted  > replied > contacted > captured
    with `stale` overlaid when a captured/contacted relationship has gone quiet
    past STALE_AFTER_DAYS.
    """
    conv = getattr(prospect, "conversion", None)
    conv_state = _clean(getattr(conv, "state", None)) if conv is not None else None

    latest = _latest_outreach(prospect)
    latest_state = _clean(getattr(latest, "state", None)) if latest is not None else None
    has_outreach = latest is not None
    replied = latest_state in _INBOUND_OUTREACH_STATES or any(
        _clean(getattr(o, "state", None)) in _INBOUND_OUTREACH_STATES
        for o in (getattr(prospect, "outreach", None) or [])
    )

    captured_at = getattr(prospect, "captured_at", None)

    # last_touch = most recent item that carries a real timestamp.
    timeline = build_timeline(prospect)
    touched = [it for it in timeline if it["occurred_at"] is not None]
    last_touch_at = touched[-1]["occurred_at"] if touched else None
    last_touch_type = touched[-1]["interaction_type"] if touched else None

    if conv_state in {"won", "partial"}:
        stage = "converted"
    elif replied:
        stage = "replied"
    elif has_outreach:
        stage = "contacted"
    else:
        stage = "captured"

    # Staleness overlay : only for not-yet-progressed relationships.
    if stage in {"captured", "contacted"} and last_touch_at is not None:
        if datetime.now(timezone.utc) - last_touch_at > timedelta(days=STALE_AFTER_DAYS):
            stage = "stale"

    event = getattr(prospect, "event", None)
    return {
        "relationship_stage": stage,
        "last_touch_at": last_touch_at,
        "last_touch_type": last_touch_type,
        "next_step": _clean(getattr(prospect, "next_step", None)),
        "contact_type": _clean(getattr(prospect, "contact_type", None)),
        "latest_outreach_status": latest_state,
        "conversion_status": conv_state,
        "source_event_id": getattr(event, "id", None),
        "source_event_title": _event_title(event),
        "has_private_note": bool(_clean(getattr(prospect, "private_note", None))),
        # who they are (LinkedIn enrichment) + how we met (capture context).
        "identity": _identity(prospect),
        "how_we_met": _how_we_met(prospect),
    }
