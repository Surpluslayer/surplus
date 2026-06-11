"""agents/drafting.py : the ONE follow-up-message composer, shared by every
surface (BookApp's /draft tap, the relationship chat, future surfaces).

Why this exists
---------------
We had two drafters: the rich one inside relationship_agent.py (voice-matched,
continues the real message thread, strips em dashes) and a stripped-down one in
book.py (name + a `next_step` string, no voice, em dashes leaking through). The
surface users actually see ("Your book today") ran the dumb one. This module is
the consolidation: a single composer that pulls the host's voice and the real
prior-message thread, so a follow-up reads like the same person continuing the
same conversation, on whichever surface drafts it.

It reuses the relationship agent's building blocks (voice examples, the
timeline->thread distiller, the dash scrub) so there is one source of truth for
"how a follow-up is written," and book.py's generic Claude-JSON caller so all
LLM calls share the same client + [book] tracing.
"""
from __future__ import annotations

import json
from typing import Optional

from . import relationships
from .book import _llm_json  # generic Claude->JSON helper (shared client + trace)
from .relationship_agent import (
    _host_voice_examples,
    _strip_dashes,
    _thread_from_timeline,
    _voice_block,
)


_FOLLOWUP_SYSTEM = (
    "You write a short follow-up message for an event host reconnecting with "
    "someone they met. CONTINUE the existing conversation in the prior messages "
    "below: pick up where it left off and reference what was actually said, then "
    "add the reason to reach out now. Never a generic cold restart. "
    "Rules: 2-4 sentences, warm and specific, never salesy. NEVER use em dashes "
    "(—) or en dashes (–); use a comma, a period, or restructure. If a "
    "<style_examples> block is provided, write in that exact voice (greeting, "
    "sign-off, sentence length, punctuation, emoji habits), matching the voice "
    "not the content. If channel is email, also return a 3-5 word subject. "
    "Return ONLY JSON: {\"subject\":\"<email only, else null>\","
    "\"body\":\"<the message>\"}"
)


def compose_followup(db, user_id: int, contact, *, reason: str,
                     channel: str = "email") -> Optional[dict]:
    """Compose one voice-matched, thread-aware follow-up to `contact` (a Contact
    ORM row owned by user_id). Returns {"subject", "body"} (subject is None off
    email), or None on any failure so the caller can fall back to its heuristic.

    `reason` is why we're reaching out now (the trigger / next step). The prior
    message thread and the host's voice are loaded here, so callers only need a
    Contact and a reason -- the same contract everywhere."""
    name = (getattr(contact, "name", None) or "there").strip() or "there"
    try:
        prior = _thread_from_timeline(relationships.contact_timeline(db, contact))
    except Exception:  # noqa: BLE001 : a timeline read failure must not break drafting
        prior = []
    voice = _host_voice_examples(db, user_id)
    system = _FOLLOWUP_SYSTEM + _voice_block(voice)
    user = (
        f"Follow up with {name}.\n"
        f"Prior conversation (oldest first; [] means no prior messages):\n"
        f"{json.dumps(prior, default=str)}\n"
        f"Reason to reach out now: {reason}\n"
        f"Channel: {channel}\n"
    )
    out = _llm_json(system, user, max_tokens=500)
    if not out or not (out.get("body") or "").strip():
        return None
    body = _strip_dashes(out["body"])
    subject = out.get("subject")
    subject = _strip_dashes(subject) if (channel == "email" and subject) else None
    return {"subject": subject, "body": body}
