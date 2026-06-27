"""Single gather path for relationship-agent + composer context.

One DB read pass produces a shared bundle; callers project agent vs composer
views instead of building parallel, diverging context dicts.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from ...spine import relationships
from .reconcile import DEFAULT_RECENT_MESSAGES, apply_to_facts
from .summary import window_and_summarize
from .... import voice

# Timeline rows that carry host<->contact conversation (not system/metadata).
_MESSAGE_SOURCE_TYPES = {"in_person_capture", "manual_note", "linkedin_outreach",
                         "email"}


def thread_from_timeline(timeline: list[dict]) -> list[dict]:
    """Distil conversational rows from a contact timeline, oldest-first."""
    thread: list[dict] = []
    for it in timeline or []:
        if it.get("source_type") not in _MESSAGE_SOURCE_TYPES:
            continue
        if (it.get("metadata") or {}).get("private"):
            continue
        text = (it.get("summary") or "").strip()
        if not text:
            continue
        direction = it.get("direction") or "none"
        who = ("host" if direction == "outbound"
               else "them" if direction == "inbound"
               else "context")
        thread.append({
            "when": it.get("occurred_at"),
            "who": who,
            "channel": it.get("channel") or it.get("source_type"),
            "text": text[:600],
        })
    return thread


# Back-compat alias used across the relationship package.
_thread_from_timeline = thread_from_timeline


def _relationship_facts(db, contact) -> dict:
    """Spine rollup fields for drafting (met_at, stage, open next_step, updates)."""
    try:
        s = relationships.contact_summary(db, contact)
    except Exception:  # noqa: BLE001
        return {}

    def _clean(v):
        x = (str(v).strip() if v is not None else "")
        return "" if x.lower() in ("", "unknown", "none") else x

    upd = s.get("latest_update") or {}
    types = [t for t in (s.get("contact_types") or []) if t and str(t).strip()]

    head = _clean(upd.get("title"))
    detail = _clean(upd.get("summary"))

    def _real_about(v):
        x = _clean(v)
        return "" if x.lower() in ("general", "general networking", "networking") else x

    ident = s.get("identity") or {}
    about = (_real_about(getattr(contact, "about", None))
             or _real_about(ident.get("works_on")) or _real_about(ident.get("bio")))
    return {
        "met_at": _clean(s.get("met_at")),
        "first_met_at": s.get("first_met_at"),
        "last_touch_at": s.get("last_touch_at"),
        "n_events": s.get("n_events") or 0,
        "stage": _clean(s.get("relationship_stage")),
        "next_step": _clean(s.get("next_step")),
        "latest_update": head or detail,
        "latest_update_detail": detail,
        "about": about[:240],
        "relationship_types": types,
    }


def _email_prior(db, user_id: int, contact, *, full_lookup: bool = False) -> list[dict]:
    """Live email bodies ({when, who, channel, text}). full_lookup also searches
    by address when no thread_id is linked yet."""
    from ..... import models
    from ... import email_sync

    try:
        user = db.get(models.User, user_id)
        account_id = getattr(user, "unipile_email_account_id", None)
        own = (getattr(user, "email_account_address", None) or "").strip().lower()
        addr = (getattr(contact, "email", None) or "").strip().lower()
        dsn = (os.environ.get("UNIPILE_DSN") or "").strip().rstrip("/")
        if dsn and not dsn.startswith(("http://", "https://")):
            dsn = f"https://{dsn}"
        api_key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
        if not (account_id and dsn and api_key):
            return []
        thread_id = getattr(contact, "email_thread_id", None)
        if not thread_id and full_lookup and addr:
            threads = email_sync.list_threads_for_address(
                dsn=dsn, api_key=api_key, account_id=account_id,
                address=addr, own_address=own)
            thread_id = threads[0]["thread_id"] if threads else None
        if not thread_id:
            return []
        msgs = email_sync.thread_messages(
            dsn=dsn, api_key=api_key, account_id=account_id,
            thread_id=str(thread_id), own_address=own, with_bodies=True)
        prior = []
        for m in msgs[-10:] if not full_lookup else msgs:
            text = (m.get("body") or "").strip()
            if not text:
                continue
            prior.append({
                "when": m.get("date"),
                "who": "host" if m.get("direction") == "out" else "them",
                "channel": "email",
                "text": (f"[{m.get('subject') or ''}] {text}"[:600]).strip(),
            })
        return prior
    except Exception:  # noqa: BLE001
        return []


def _voice_block(db, user_id: int, channel: str) -> str:
    from ..... import models
    try:
        user = db.get(models.User, user_id)
    except Exception:  # noqa: BLE001
        user = None
    vch = "email" if channel == "email" else "linkedin"
    return voice.build_voice_context(
        user, channel=vch, message_type="warm_followup")["block"]


def gather_contact_context(
    db,
    user_id: int,
    contact,
    *,
    channel: str = "linkedin",
    voice_block: Optional[str] = None,
    recent_messages: int = DEFAULT_RECENT_MESSAGES,
    merge_email: bool = True,
) -> dict[str, Any]:
    """One gather pass: timeline, full thread, windowed thread, spine facts."""
    name = (getattr(contact, "name", None) or "there").strip() or "there"

    try:
        timeline = relationships.contact_timeline(db, contact)
    except Exception:  # noqa: BLE001
        timeline = []

    prior_full = thread_from_timeline(timeline)
    if merge_email and getattr(contact, "email_thread_id", None):
        email_msgs = _email_prior(db, user_id, contact)
        if email_msgs:
            prior_full = sorted(
                prior_full + email_msgs,
                key=lambda m: str(m.get("when") or ""))
    if channel == "email":
        extra = _email_prior(db, user_id, contact, full_lookup=True)
        if extra:
            prior_full = sorted(
                prior_full + extra,
                key=lambda m: str(m.get("when") or ""))

    facts = apply_to_facts(_relationship_facts(db, contact), prior_full)
    summary = dict(relationships.contact_summary(db, contact))
    summary["next_step"] = facts.get("next_step")

    prior, thread_summary = window_and_summarize(
        prior_full, recent_messages,
        db=db, user_id=user_id, contact=contact)

    if voice_block is None:
        voice_block = _voice_block(db, user_id, channel)

    register = voice.detect_register(
        [m.get("text") or "" for m in prior_full if m.get("who") == "them"])

    def _real(v):
        s = (v or "").strip()
        return "" if s.lower() == "unknown" else s

    from ...spine.memory import draft_grounding
    store_asserted, store_optional, store_provenance = draft_grounding(
        db, getattr(contact, "id", None))

    try:
        events = relationships.contact_events(db, contact)
    except Exception:  # noqa: BLE001
        events = []

    return {
        "name": name,
        "company": _real(getattr(contact, "company", None)),
        "role": (_real(getattr(contact, "title", None))
                 or _real(getattr(contact, "headline", None))),
        "timeline": timeline,
        "prior_full": prior_full,
        "prior": prior,
        "thread_summary": thread_summary,
        "summary": summary,
        "events": events,
        "facts": facts,
        "register": register,
        "voice_block": voice_block,
        "store_grounding": store_asserted,
        "store_optional": store_optional,
        "store_provenance": store_provenance,
    }


def as_agent_context(gathered: dict) -> dict:
    """Phase-2 decision model view."""
    out = {
        "summary": gathered.get("summary") or {},
        "events": gathered.get("events") or [],
        "timeline": (gathered.get("timeline") or [])[-12:],
        "prior_messages": gathered.get("prior") or [],
        "prior_messages_full": gathered.get("prior_full") or [],
    }
    if gathered.get("thread_summary"):
        out["thread_summary"] = gathered["thread_summary"]
    return out


def as_composer_context(gathered: dict) -> dict:
    """Shared follow-up composer view."""
    return {
        "name": gathered.get("name"),
        "company": gathered.get("company"),
        "role": gathered.get("role"),
        "prior": gathered.get("prior") or [],
        "prior_full": gathered.get("prior_full") or [],
        "thread_summary": gathered.get("thread_summary"),
        "register": gathered.get("register"),
        "facts": gathered.get("facts") or {},
        "store_grounding": gathered.get("store_grounding") or [],
        "store_optional": gathered.get("store_optional") or [],
        "store_provenance": gathered.get("store_provenance") or [],
        "voice_block": gathered.get("voice_block") or "",
    }
