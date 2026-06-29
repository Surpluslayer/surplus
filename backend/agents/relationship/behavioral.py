"""agents/relationship/behavioral.py : deterministic per-contact behavioral
signals, derived from stored messages (no LLM, no network).

These are META facts: they inform HOW / WHERE to reach someone, not WHAT to say.
So they're written into the fact store like any other fact, but excluded from
draft GROUNDING (you don't "mention" someone's channel preference in a message --
you act on it). The standout is `channel_preference`: which channel a contact
actually responds on (CLAW's "channel_strictness"), learned from their inbound
messages instead of asked for.

Derived from the timeline the system already stores, so this is pure mode-B->A
distillation: read raw history, upsert a structured signal.
"""
from __future__ import annotations

from typing import Optional

from .pipeline.context.gather import thread_from_timeline as _thread_from_timeline
from .spine.relationships import contact_timeline
from .spine.memory import upsert_fact

# Real two-way messaging channels. A capture/note/manual row isn't a channel
# someone "replies on", so it doesn't count toward channel preference.
_MESSAGING = {"linkedin", "email", "whatsapp", "imessage", "sms"}


def derive_channel_preference(db, contact, *, commit: bool = False) -> Optional[str]:
    """Which channel this contact responds on, from their INBOUND messages: the
    channel of their most recent inbound message (the freshest "where they're
    active" signal). Upserts a `channel_preference` META fact (source='behavior')
    and returns the channel, or None when there's no inbound signal yet.

    Best-effort: any failure is swallowed -- a derived signal must never break the
    refresh that calls it."""
    try:
        thread = _thread_from_timeline(contact_timeline(db, contact))
        inbound = [m for m in thread
                   if m.get("who") == "them" and m.get("channel") in _MESSAGING]
        if not inbound:
            return None
        latest = max(inbound, key=lambda m: str(m.get("when") or ""))
        ch = latest.get("channel")
        if ch:
            upsert_fact(db, contact.user_id, contact.id, "channel_preference", ch,
                        source="behavior", confidence="high", commit=commit)
        return ch
    except Exception as exc:  # noqa: BLE001 - derived signal, never fatal
        print(f"  [behavioral] channel pref skipped: {type(exc).__name__}: {exc}")
        return None
