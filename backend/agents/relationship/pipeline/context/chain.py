"""context/chain.py : resolve the active CHAIN to continue (the "right chain").

The deterministic answer to "where do we reply, and in which thread?" so the agent
continues the existing conversation instead of starting a new one. Channel priority:

  1. where they're actually active  -> derive_channel_preference (most-recent INBOUND msg)
  2. the stored channel_preference fact (behavioral)
  3. identity availability           -> email > linkedin > phone(imessage)
  4. caller fallback

Then it attaches the thread to continue for that channel (email -> the linked
email_thread_id) and the handle to send to. Pure read; best-effort (never raises).
"""
from __future__ import annotations

from typing import Optional

# Channels we can hold a back-and-forth on.
_MESSAGING = ("email", "linkedin", "whatsapp", "imessage", "sms")


def _has_identity(contact, channel: str) -> bool:
    if channel == "email":
        return bool(getattr(contact, "email", None))
    if channel == "linkedin":
        return bool(getattr(contact, "linkedin_url", None)
                    or getattr(contact, "linkedin_public_id", None))
    if channel in ("imessage", "sms", "whatsapp"):
        return bool(getattr(contact, "phone", None))
    return False


def _handle_for(contact, channel: str) -> str:
    if channel == "email":
        return getattr(contact, "email", "") or ""
    if channel in ("imessage", "sms", "whatsapp"):
        return getattr(contact, "phone", "") or ""
    if channel == "linkedin":
        return (getattr(contact, "linkedin_url", "")
                or getattr(contact, "linkedin_public_id", "") or "")
    return ""


def _identity_channel(contact, fallback: str) -> str:
    for ch in ("email", "linkedin", "imessage"):
        if _has_identity(contact, ch):
            return ch
    return fallback


def _preferred(db, contact) -> Optional[str]:
    """Where they're active: live derivation (most-recent inbound), else the stored
    channel_preference fact. None if no signal."""
    try:
        from ...behavioral import derive_channel_preference
        ch = derive_channel_preference(db, contact)
        if ch:
            return ch
    except Exception:  # noqa: BLE001
        pass
    try:
        from ...spine.memory import get_facts
        facts = get_facts(db, getattr(contact, "id", None), key="channel_preference")
        if facts:
            return (facts[0].value or "").strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_active_chain(db, contact, *, fallback: str = "linkedin") -> dict:
    """{channel, thread_id, to_handle, reason} -- the chain to CONTINUE. Picks the
    channel they're active on (when we can reach them there), else an available
    identity, else `fallback`; attaches the email thread to reply into."""
    pref = _preferred(db, contact)
    if pref and _has_identity(contact, pref):
        channel, reason = pref, "active_channel"
    else:
        channel = _identity_channel(contact, fallback)
        reason = "identity" if _has_identity(contact, channel) else "fallback"

    thread_id = None
    if channel == "email":
        thread_id = getattr(contact, "email_thread_id", None)

    return {"channel": channel, "thread_id": thread_id,
            "to_handle": _handle_for(contact, channel), "reason": reason}
