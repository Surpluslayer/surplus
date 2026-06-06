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

# Channel inferred from a stored interaction's source_type when it doesn't carry
# one explicitly. Keeps the timeline item shape uniform across derived + stored.
_CHANNEL_BY_SOURCE = {
    "manual_note": "manual",
    "email_interaction": "email",
    "calendar_meeting": "calendar",
    "relationship_interaction": "manual",
    "draft_generated": "manual",
}


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


def _interaction_item(ri: Any) -> dict:
    """Map a stored RelationshipInteraction to the timeline item shape."""
    import json
    meta: dict = {}
    raw = getattr(ri, "meta_json", None)
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                meta = decoded
        except (ValueError, TypeError):
            meta = {}
    source_type = _clean(getattr(ri, "source_type", None)) or "relationship_interaction"
    meta = {**meta, "visibility": _clean(getattr(ri, "visibility", None)) or "private",
            "interaction_id": getattr(ri, "id", None)}
    return _item(
        source_type,
        _clean(getattr(ri, "interaction_type", None)) or "interaction",
        getattr(ri, "occurred_at", None),
        title=_clean(getattr(ri, "title", None)) or "",
        summary=_clean(getattr(ri, "summary", None)) or "",
        channel=_CHANNEL_BY_SOURCE.get(source_type, "manual"),
        direction=_clean(getattr(ri, "direction", None)) or "none",
        **meta,
    )


def build_timeline(prospect: Any, interactions: Any = None) -> list[dict]:
    """Assemble the chronological (oldest-first) relationship timeline for one
    Prospect. Unions two sources:

      1. derived items reconstructed from existing persisted data (capture
         metadata, OutreachLog, Conversion), and
      2. stored RelationshipInteraction rows passed in via `interactions`
         (manual notes, email, calendar) — fetch them with fetch_interactions.

    Pure read; never raises on missing optional fields. When `interactions` is
    None this is exactly the Milestone-1 derived-only timeline."""
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

    # ── stored interactions (manual notes, email, calendar, intros) ───
    for ri in (interactions or []):
        items.append(_interaction_item(ri))

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


def relationship_summary(prospect: Any, interactions: Any = None) -> dict:
    """Deterministic, ML-free snapshot of where this relationship stands.

    Stage precedence (strongest signal wins):
        converted  > replied > contacted > captured
    with `stale` overlaid when a captured/contacted relationship has gone quiet
    past STALE_AFTER_DAYS. `interactions` (stored RelationshipInteraction rows)
    contribute to last_touch but not to stage classification.
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
    timeline = build_timeline(prospect, interactions)
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


# ── draft context (Milestone 4) ──────────────────────────────────────────


def _relative_age(dt: Optional[datetime]) -> Optional[str]:
    """Human "5 days ago" / "today" for a timestamp. None when unknown."""
    dt = _as_aware(dt)
    if dt is None:
        return None
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def relationship_context(prospect: Any, interactions: Any = None,
                         max_recent: int = 3) -> Optional[str]:
    """A COMPACT, outbound-safe relationship brief for grounding a draft.

    Returns a short plain-text block (the "good draft context" shape) or None
    when there's no meaningful history yet (a brand-new capture). Designed to be
    fed into compose() as background, NOT quoted verbatim.

    SAFETY: deliberately omits the operator-only private_note and any timeline
    item flagged private/team-internal — this block can influence outbound copy,
    so it must never carry internal-only annotations. Bounded to a handful of
    lines so we never dump a whole timeline into the prompt.

    Integration points:
      - in-person /scan (routes/inperson.py) — wired.
      - the post-accept auto-DM (routes/webhooks._trigger_auto_dm) and the
        future draft route (PR #215 agents/draft.py) — pass the result as
        compose(..., relationship_ctx=relationship_context(p, ...)). TODO once
        those paths can cheaply fetch interactions.
    """
    summary = relationship_summary(prospect, interactions)
    stage = summary["relationship_stage"]

    # No meaningful history : a bare/fresh capture with nothing to add.
    meaningful = (
        stage not in {"captured"}
        or summary["next_step"] or summary["contact_type"]
        or summary["latest_outreach_status"] or summary["conversion_status"]
        or bool(interactions)
    )
    if not meaningful:
        return None

    lines = ["PRIOR RELATIONSHIP (background only, do not quote verbatim):"]
    if summary["source_event_title"]:
        lines.append(f"- Captured at {summary['source_event_title']}")
    if summary["contact_type"]:
        lines.append(f"- Contact type: {summary['contact_type']}")
    if summary["next_step"]:
        lines.append(f"- Planned next step: {summary['next_step']}")
    if summary["last_touch_type"]:
        age = _relative_age(summary["last_touch_at"])
        touch = summary["last_touch_type"].replace("_", " ")
        lines.append(f"- Last touch: {touch}" + (f" ({age})" if age else ""))
    lines.append(f"- Conversion state: {summary['conversion_status'] or 'none'}")
    lines.append(f"- Relationship stage: {stage}")

    # A few recent, non-private timeline summaries for texture.
    recent = [
        it for it in build_timeline(prospect, interactions)
        if it["occurred_at"] is not None
        and not it["metadata"].get("private")
        and (it["summary"] or "").strip()
    ]
    if recent:
        lines.append("- Recent touches:")
        for it in recent[-max_recent:]:
            lines.append(f"    · {(it['summary'] or '').strip()[:120]}")
    return "\n".join(lines)


# ── DB-aware helpers (Milestone 3) ───────────────────────────────────────
# These touch the database; the pure functions above do not. Kept here so the
# whole relationship read/write surface lives in one module.


def fetch_interactions(db, prospect) -> list:
    """All stored RelationshipInteraction rows relevant to one Prospect : those
    tied to the prospect directly, plus those tied to its linked Contact (so a
    note logged against the durable person shows on every per-event timeline).
    Returns [] on any error — a broken read must never sink a timeline."""
    from .. import models
    try:
        clauses = [models.RelationshipInteraction.prospect_id == prospect.id]
        contact_id = getattr(prospect, "contact_id", None)
        if contact_id is not None:
            clauses.append(models.RelationshipInteraction.contact_id == contact_id)
        from sqlalchemy import or_
        return (db.query(models.RelationshipInteraction)
                  .filter(or_(*clauses))
                  .all())
    except Exception:  # noqa: BLE001
        return []


def link_contact(db, prospect, owner_user_id: int):
    """Lazily find-or-create the Contact for a Prospect and link it.

    Idempotent and conservative : only links when a STRONG identity (LinkedIn
    slug / email) is derivable — no fuzzy/name dedup. Returns the Contact, or
    None when there's no stable identity to key on (the Prospect simply stays
    contact-less, which every flow supports). Never raises."""
    from .. import models
    from ..triage.enrichment_cache import identity_keys
    try:
        if getattr(prospect, "contact_id", None) is not None:
            return db.get(models.Contact, prospect.contact_id)

        keys = identity_keys(
            email=_clean(getattr(prospect, "email", None)) or "",
            linkedin_url=_clean(getattr(prospect, "linkedin_url", None)) or "",
        )
        if not keys:
            return None
        primary = keys[0]

        contact = (db.query(models.Contact)
                     .filter_by(user_id=owner_user_id, primary_identity_key=primary)
                     .first())
        if contact is None:
            contact = models.Contact(
                user_id=owner_user_id,
                primary_identity_key=primary,
                name=_clean(getattr(prospect, "name", None)),
                linkedin_url=_clean(getattr(prospect, "linkedin_url", None)),
                linkedin_public_id=_clean(getattr(prospect, "linkedin_provider_id", None)),
                company=_clean(getattr(prospect, "company", None)),
            )
            db.add(contact)
            db.flush()  # assign contact.id without committing the caller's tx

        prospect.contact_id = contact.id
        db.commit()
        db.refresh(contact)
        return contact
    except Exception:  # noqa: BLE001
        db.rollback()
        return None


def add_note(db, prospect, owner_user_id: int, summary: str,
             title: str = "Note", visibility: str = "private"):
    """Record a manual note as a stored RelationshipInteraction, linking the
    Contact spine opportunistically. Returns the created row."""
    from .. import models
    contact = link_contact(db, prospect, owner_user_id)
    vis = visibility if visibility in {"private", "team"} else "private"
    ri = models.RelationshipInteraction(
        actor_user_id=owner_user_id,
        prospect_id=prospect.id,
        contact_id=getattr(contact, "id", None),
        event_id=getattr(prospect, "event_id", None),
        source_type="manual_note",
        interaction_type="note",
        direction="none",
        occurred_at=datetime.now(timezone.utc),
        title=(title or "Note").strip()[:200],
        summary=(summary or "").strip(),
        visibility=vis,
    )
    db.add(ri)
    db.commit()
    db.refresh(ri)
    return ri


# ── contact-centric read model (the durable-person projection) ───────────
# Everything above answers "what is this ONE per-event record (Prospect)?" The
# functions below answer "what is this DURABLE PERSON (Contact)?" by rolling up
# every linked Prospect. Surplus's atomic unit is the shared event, so a Contact
# is a *projection* over its per-event Prospect rows — never a parallel store.
# Event/Prospect/OutreachLog stay the source of truth; this is a read view.

# Strength ordering for the strongest stage a relationship has reached across all
# the events we've shared with one person. Higher wins. "stale" is an overlay (a
# fresh event un-stales someone), so it ranks below any live stage.
_STAGE_RANK = {
    "stale": 0,
    "captured": 1,
    "contacted": 2,
    "replied": 3,
    "converted": 4,
}

# Sorts never-touched rows to the END when ordering newest-touch-first.
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _strongest_stage(stages) -> Optional[str]:
    best, best_rank = None, -1
    for s in stages:
        r = _STAGE_RANK.get(s, -1)
        if r > best_rank:
            best, best_rank = s, r
    return best


def list_contacts(db, user_id: int) -> list:
    """Every durable Contact owned by one user (the 'who I've met' inventory).
    Returns [] on any error so a broken read never sinks the page.

    Eager-loads the whole read tree (prospects -> event / outreach / conversion)
    in a handful of batched queries instead of lazy-loading per row. The
    contact-centric rollup (contact_summary -> contact_events ->
    relationship_summary) touches every one of these per prospect, so without
    this the page fires ~5 queries PER prospect (N+1) and a user with dozens of
    contacts waits tens of seconds. selectinload keeps it to ~5 queries total."""
    from .. import models
    from sqlalchemy.orm import selectinload
    try:
        return (db.query(models.Contact)
                  .filter(models.Contact.user_id == user_id)
                  .options(
                      selectinload(models.Contact.prospects)
                        .selectinload(models.Prospect.event),
                      selectinload(models.Contact.prospects)
                        .selectinload(models.Prospect.outreach),
                      selectinload(models.Contact.prospects)
                        .selectinload(models.Prospect.conversion),
                  )
                  .all())
    except Exception:  # noqa: BLE001
        return []


def fetch_contact_interactions(db, contact) -> list:
    """Every stored RelationshipInteraction for a durable Contact, de-duped.

    A note may be tied to the Contact directly (contact_id) OR to any of its
    linked per-event Prospect rows (prospect_id). We union both and de-dup by
    interaction id, so a contact-scoped note shows up once — not once per linked
    Prospect. Returns [] on any error."""
    from .. import models
    from sqlalchemy import or_
    try:
        prospect_ids = [p.id for p in (getattr(contact, "prospects", None) or [])]
        clauses = [models.RelationshipInteraction.contact_id == contact.id]
        if prospect_ids:
            clauses.append(
                models.RelationshipInteraction.prospect_id.in_(prospect_ids))
        rows = (db.query(models.RelationshipInteraction)
                  .filter(or_(*clauses))
                  .all())
        seen, deduped = set(), []
        for ri in rows:
            rid = getattr(ri, "id", None)
            if rid in seen:
                continue
            seen.add(rid)
            deduped.append(ri)
        return deduped
    except Exception:  # noqa: BLE001
        return []


def prefetch_interactions_by_prospect(db, contacts) -> dict:
    """Batch fetch_interactions for a whole set of contacts into ONE query.

    fetch_interactions(db, p) is a per-prospect round-trip; called once per
    linked prospect across a contacts list it's the second half of the N+1 (the
    first half — prospects/event/outreach/conversion — is killed by the
    selectinload in list_contacts). This mirrors its exact union semantics
    (RelationshipInteraction tied to the prospect directly OR to its Contact),
    deduped by interaction id, but resolves the whole list in a single SELECT.

    Returns {prospect_id: [RelationshipInteraction, ...]}. Pass the result into
    contact_summary/contact_events as `interactions_by_prospect`. [] on error so
    a broken read still degrades to an empty timeline, never a 500."""
    from .. import models
    from sqlalchemy import or_
    prospects = [p for c in contacts
                 for p in (getattr(c, "prospects", None) or [])]
    prospect_ids = [p.id for p in prospects if getattr(p, "id", None) is not None]
    contact_ids = [c.id for c in contacts if getattr(c, "id", None) is not None]
    if not prospect_ids and not contact_ids:
        return {}
    try:
        clauses = []
        if prospect_ids:
            clauses.append(
                models.RelationshipInteraction.prospect_id.in_(prospect_ids))
        if contact_ids:
            clauses.append(
                models.RelationshipInteraction.contact_id.in_(contact_ids))
        rows = (db.query(models.RelationshipInteraction)
                  .filter(or_(*clauses))
                  .all())
    except Exception:  # noqa: BLE001
        return {}

    by_prospect: dict = {}
    by_contact: dict = {}
    for ri in rows:
        pid = getattr(ri, "prospect_id", None)
        cid = getattr(ri, "contact_id", None)
        if pid is not None:
            by_prospect.setdefault(pid, []).append(ri)
        if cid is not None:
            by_contact.setdefault(cid, []).append(ri)

    result: dict = {}
    for p in prospects:
        merged, seen = [], set()
        candidates = (by_prospect.get(p.id, [])
                      + by_contact.get(getattr(p, "contact_id", None), []))
        for ri in candidates:
            rid = getattr(ri, "id", None)
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(ri)
        result[p.id] = merged
    return result


def contact_events(db, contact, interactions_by_prospect=None) -> list[dict]:
    """One row per shared event (per linked Prospect) : where we met / re-touched
    this person, plus where that per-event relationship stands. The 'events we've
    shared' breakdown behind a Contact, newest touch first.

    `interactions_by_prospect` (from prefetch_interactions_by_prospect) lets the
    list view avoid a per-prospect interaction query; when None we fall back to
    the per-prospect fetch (the single-contact detail path stays unchanged)."""
    rows = []
    for p in (getattr(contact, "prospects", None) or []):
        event = getattr(p, "event", None)
        if interactions_by_prospect is not None:
            inter = interactions_by_prospect.get(getattr(p, "id", None), [])
        else:
            inter = fetch_interactions(db, p)
        summary = relationship_summary(p, inter)
        rows.append({
            "prospect_id": getattr(p, "id", None),
            "event_id": getattr(event, "id", None),
            "event_title": _event_title(event),
            "event_city": _clean(getattr(event, "city", None)),
            "relationship_stage": summary["relationship_stage"],
            "captured_at": _as_aware(getattr(p, "captured_at", None)),
            "last_touch_at": summary["last_touch_at"],
            "status": _clean(getattr(p, "status", None)),
            "connection_status": _clean(getattr(p, "connection_status", None)),
            "contact_type": summary["contact_type"],
            "next_step": summary["next_step"],
        })
    rows.sort(key=lambda r: r["last_touch_at"] or _MIN_DT, reverse=True)
    return rows


def contact_summary(db, contact, interactions_by_prospect=None) -> dict:
    """A durable-person rollup across every event we've shared : who they are,
    when we first met, how many events, the strongest stage reached, the freshest
    touch, and the open next step. The Pillar-1 'who I've met' card.

    `interactions_by_prospect` is the batched index from the list view; None on
    the single-contact path (which keeps its per-prospect fetch)."""
    prospects = list(getattr(contact, "prospects", None) or [])
    events = contact_events(db, contact, interactions_by_prospect)

    stages = [e["relationship_stage"] for e in events]
    first_touches = [e["captured_at"] for e in events if e["captured_at"]]
    last_touches = [e["last_touch_at"] for e in events if e["last_touch_at"]]
    next_steps = [e["next_step"] for e in events if e["next_step"]]
    contact_types = [e["contact_type"] for e in events if e["contact_type"]]
    connected = any(e["connection_status"] == "connected" for e in events)

    # Identity : prefer a linked Prospect carrying real enrichment, else the
    # first one; fall back to the Contact's own stored fields.
    identity: dict = {}
    for p in prospects:
        cand = _identity(p)
        identity = identity or cand
        if cand.get("headline") or cand.get("company"):
            identity = cand
            break

    return {
        "contact_id": getattr(contact, "id", None),
        "name": _clean(getattr(contact, "name", None)) or identity.get("name"),
        "company": _clean(getattr(contact, "company", None)) or identity.get("company"),
        "linkedin_url": _clean(getattr(contact, "linkedin_url", None)),
        "primary_identity_key": _clean(getattr(contact, "primary_identity_key", None)),
        "identity": identity,
        "is_connection": connected,
        "n_events": len(events),
        "first_met_at": min(first_touches) if first_touches else None,
        "last_touch_at": max(last_touches) if last_touches else None,
        "relationship_stage": _strongest_stage(stages),
        "contact_types": sorted({c for c in contact_types}),
        "next_step": next_steps[0] if next_steps else None,
        "event_ids": [e["event_id"] for e in events if e["event_id"] is not None],
    }


def contact_timeline(db, contact) -> list[dict]:
    """The unified chronological (oldest-first) timeline for a durable person :
    every derived touch from each linked per-event Prospect (annotated with which
    event it came from), unioned with the contact's stored interactions fetched
    ONCE.

    Each per-event timeline is built with interactions=None and the contact-level
    interactions are added a single time, so a contact-scoped note appears once,
    not once per linked Prospect."""
    items: list[dict] = []
    for p in (getattr(contact, "prospects", None) or []):
        event = getattr(p, "event", None)
        eid = getattr(event, "id", None)
        etitle = _event_title(event)
        for it in build_timeline(p, None):
            it["metadata"] = {
                **it["metadata"],
                "event_id": eid,
                "event_title": etitle,
                "prospect_id": getattr(p, "id", None),
            }
            items.append(it)

    for ri in fetch_contact_interactions(db, contact):
        items.append(_interaction_item(ri))

    items.sort(key=lambda it: (
        it["occurred_at"] or _FAR_FUTURE,
        _SOURCE_RANK.get(it["source_type"], 99),
    ))
    return items
