"""agents/relationship/cadence.py : who's overdue for a touch, ranked.

The DATED trigger engine (triggers.py + ContactFact.due_date) fires on specific
calendar events -- a birthday, a flight. Cadence is the OTHER half of the proactive
surface: relationship MAINTENANCE. People you haven't reached in longer than they
warrant, even with no specific event on the calendar. Pure deterministic logic over
the derived `last_touch_at` + the contact's `vip` flag + relationship stage -- no
ML, no network.

Expected cadence (days before a relationship is "overdue" for a touch):
    VIP (starred)                            -> CADENCE_VIP
    active two-way (replied / converted)     -> CADENCE_ACTIVE
    one-way / just-met (contacted / captured)-> CADENCE_LOOSE
A contact with no recorded touch is skipped -- there's no relationship to maintain
yet (that's the cold-outreach surface, not cadence).

This is the DECISION layer (who's due); whether an automated nudge actually fires is
gated separately by sender.automated_send_enabled(channel).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ...spine import relationships

# How many days before a relationship is due for a touch. Tunable policy, not data.
CADENCE_VIP = 30        # starred / important: keep these warm
CADENCE_ACTIVE = 90     # a real two-way relationship
CADENCE_LOOSE = 180     # one-way or barely-met: a light, infrequent check-in

_ACTIVE_STAGES = {"replied", "converted"}


def cadence_days(contact, summary: dict) -> int:
    """Expected touch interval for this relationship. VIP wins; then a real two-way
    relationship gets the standard cadence; a one-way / just-met contact gets the
    loose cadence."""
    if bool(getattr(contact, "vip", False)):
        return CADENCE_VIP
    stage = (summary.get("relationship_stage") or "").lower()
    if stage in _ACTIVE_STAGES:
        return CADENCE_ACTIVE
    return CADENCE_LOOSE


def _reason(name: str, vip: bool, days_since: int, cad: int) -> str:
    who = name or "This contact"
    if vip:
        return f"{who} is a VIP -- {days_since}d since last touch (cadence {cad}d)"
    return f"{who}: {days_since}d since last touch (cadence {cad}d)"


def _row(contact, summary: dict, *, days_since: int, cad: int) -> dict:
    vip = bool(getattr(contact, "vip", False))
    return {
        "contact_id": summary.get("contact_id"),
        "name": summary.get("name"),
        "company": summary.get("company"),
        "vip": vip,
        "relationship_stage": summary.get("relationship_stage"),
        "last_touch_at": summary.get("last_touch_at"),
        "next_step": summary.get("next_step"),
        "days_since": days_since,
        "cadence_days": cad,
        "overdue_days": days_since - cad,
        # Ratio normalizes urgency across different cadences (1.0 == exactly due):
        # a VIP 35d out (1.17) outranks an acquaintance 100d out (0.56).
        "overdue_ratio": round(days_since / cad, 3) if cad else None,
        "reason": _reason(summary.get("name") or "", vip, days_since, cad),
    }


def due_contacts(db, user_id: int, *, now: Optional[datetime] = None,
                 within_days: int = 0, limit: Optional[int] = None) -> list[dict]:
    """Owned contacts overdue for a touch, MOST-OVERDUE first.

    `within_days` looks AHEAD: include contacts that come due within N days
    (days_since + within_days >= cadence), so a daily sweep can surface them a touch
    early. Contacts never touched are skipped. Deterministic; returns [] on any read
    error so a broken page never 500s."""
    now = now or datetime.now(timezone.utc)
    try:
        contacts = relationships.list_contacts(db, user_id)
    except Exception:  # noqa: BLE001 : a broken read must not sink the surface
        return []
    if not contacts:
        return []
    inter_index = relationships.prefetch_interactions_by_prospect(db, contacts)
    update_index = relationships.prefetch_activity_updates_by_contact(db, contacts)
    snoozed = _snoozed_contact_ids(db, user_id, now=now)

    # Pre-product cutoff: the chat syncs backfill YEARS of history, and a
    # conversation that ended before the host even joined surplus is context,
    # not a to-do. A contact whose last touch predates the host's account is
    # skipped until a product-era touch moves it forward -- EXCEPT when the
    # host starred them (VIP = an explicit "keep this one warm").
    product_start = None
    try:
        from ..... import models as _models
        owner = db.get(_models.User, user_id)
        product_start = getattr(owner, "created_at", None)
        if product_start is not None and product_start.tzinfo is None:
            product_start = product_start.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 : anchor is best-effort, never sinks the feed
        product_start = None

    out: list[dict] = []
    for c in contacts:
        if c.id in snoozed:
            continue  # dismissed "not now" -> suppressed until the snooze expires
        s = relationships.contact_summary(db, c, inter_index, update_index.get(c.id))
        lt = s.get("last_touch_at")
        if lt is None:
            continue  # never touched -> not a maintenance candidate
        if lt.tzinfo is None:                       # naive rows -> assume UTC
            lt = lt.replace(tzinfo=timezone.utc)
        if (product_start is not None and lt < product_start
                and not bool(getattr(c, "vip", False))):
            continue  # relationship predates surplus -> history, not maintenance
        days_since = (now - lt).days
        cad = cadence_days(c, s)
        if days_since + max(within_days, 0) < cad:
            continue
        out.append(_row(c, s, days_since=days_since, cad=cad))

    out.sort(key=lambda r: (r["overdue_ratio"] or 0.0), reverse=True)
    return out[:limit] if limit else out


# ── snooze / dismiss ──────────────────────────────────────────────────────────
# A snooze suppresses a contact from the CADENCE feed (relationship maintenance)
# until a date -- "not now", without sending. Stored as a ContactFact whose VALUE is
# the until-timestamp (NOT due_date, so it never leaks into the dated-trigger feed).
# Cadence-only: a snoozed contact's birthday trigger still surfaces.
_SNOOZE_KEY = "cadence_snooze"


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat((s or "").strip())
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _snoozed_contact_ids(db, user_id: int, *, now: datetime) -> set:
    """Contact ids with an ACTIVE snooze (until-value still in the future).
    Best-effort: a read failure just means nothing is suppressed."""
    from ..... import models
    try:
        rows = (db.query(models.ContactFact)
                .filter(models.ContactFact.user_id == user_id,
                        models.ContactFact.key == _SNOOZE_KEY).all())
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for r in rows:
        until = _parse_iso(r.value)
        if until is not None and until > now:
            out.add(r.contact_id)
    return out


def snooze_contact(db, user_id: int, contact_id: int, *, days: int = 30,
                   now: Optional[datetime] = None) -> dict:
    """Suppress a contact from the cadence feed until now+days. Idempotent (upsert)."""
    from datetime import timedelta
    from ...spine.memory import upsert_fact
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(days=max(days, 1))
    upsert_fact(db, user_id, contact_id, _SNOOZE_KEY, until.isoformat(),
                source="user", confidence="high")
    return {"contact_id": contact_id, "snoozed_until": until.isoformat(), "days": days}


def unsnooze_contact(db, user_id: int, contact_id: int) -> bool:
    """Clear a snooze so the contact can surface in the cadence feed again."""
    from ...spine.memory import delete_fact
    return delete_fact(db, contact_id, _SNOOZE_KEY)
