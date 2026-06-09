"""Shared voice layer: the single boundary between RAW stored voice data
(``User.voice_examples`` / the ``OPERATOR_VOICE_EXAMPLES`` env fallback) and the
MODEL-READY voice context both drafting surfaces inject into their prompts.

Two surfaces speak in the host's voice and must not drift apart:
  - the cold-DM composer (``agents/outreach.py``, Claude Haiku)
  - the follow-up relationship agent (``agents/relationship_agent.py``, Sonnet)

Historically each surface copy-pasted the same JSON-parse + ``[:8]`` cap + env
fallback, and rendered its own ``<style_examples>`` block. This module owns that
mechanism so the two stay consistent and later voice work is written once.

Scope discipline (see the staged voice plan):
  - Step 0 (this module, today) is BEHAVIOR-PRESERVING: it centralizes the parse
    logic and the follow-up agent's existing block format. It does NOT change any
    rendered string, does NOT touch the em-dash scrubbers (the two surfaces use
    intentionally different ones), and does NOT alter the cold-DM block wording.
  - Step 2 adds a structured ``host_voice_profile`` (``profile`` is ``None`` here).
  - Step 4 adds channel/message_type-scoped retrieval. ``build_voice_context``
    already ACCEPTS ``channel``/``message_type`` (ignored for now) so adding that
    later needs no signature change at the call sites.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


# ── Raw → list[str] ──────────────────────────────────────────────────────────

def parse_voice_examples(raw: Optional[str], *, env_fallback: bool = True,
                         limit: int = 8) -> list[str]:
    """Parse a raw JSON string of voice examples into a clean, capped list.

    This is the exact logic both surfaces duplicated:
      - empty ``raw`` falls back to ``OPERATOR_VOICE_EXAMPLES`` (when
        ``env_fallback``), else returns ``[]``
      - bad JSON or a non-list parses to ``[]`` (a typo can never break a run)
      - each element is coerced to ``str`` and stripped; blanks dropped
      - capped at ``limit`` (default 8) to bound input tokens
    """
    raw = (raw or "").strip()
    if not raw and env_fallback:
        raw = (os.environ.get("OPERATOR_VOICE_EXAMPLES") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(s).strip() for s in parsed if str(s).strip()][:limit]


def resolve_voice_examples_for_user(user: Any, *, limit: int = 8) -> list[str]:
    """Resolve a host's voice examples from their ``User`` row, then env.

    Defensive: accessing ``user.voice_examples`` can raise
    ``DetachedInstanceError`` when the row's session has closed (the background
    prefetch path), so any attribute failure is swallowed and we fall through to
    the env fallback inside :func:`parse_voice_examples`.
    """
    raw = ""
    try:
        if user is not None:
            raw = (getattr(user, "voice_examples", "") or "").strip()
    except Exception:  # noqa: BLE001 - DetachedInstanceError + friends
        raw = ""
    return parse_voice_examples(raw, env_fallback=True, limit=limit)


# ── list[str] → model-ready <style_examples> block ───────────────────────────
# This is the follow-up agent's existing format, verbatim, now shared. The
# cold-DM composer keeps its own (differently-worded) inline block until Step 2
# deliberately unifies the wording.

_STYLE_HEADER = (
    "Past messages this host actually sent. Match their VOICE — greeting, "
    "sign-off, sentence length, formality, punctuation and emoji habits — "
    "not the content:"
)


def build_style_examples_block(examples: list[str], *, header: str = _STYLE_HEADER) -> str:
    """Render examples as a ``<style_examples>`` block, or ``""`` when there are
    none. Byte-for-byte identical to the follow-up agent's prior ``_voice_block``
    output (leading newline included) so wiring it in changes nothing."""
    if not examples:
        return ""
    lines = ["", "<style_examples>", header]
    for i, ex in enumerate(examples, 1):
        lines.append(f"---\nExample {i}:\n{ex}")
    lines += ["---", "</style_examples>"]
    return "\n".join(lines)


# ── The seam both surfaces will converge on (Step 2 fills in ``profile``) ─────

def build_voice_context(user: Any, *, channel: Optional[str] = None,
                        message_type: Optional[str] = None,
                        limit: int = 8) -> dict:
    """Resolve everything a draft call needs to speak in the host's voice.

    Returns ``{"profile", "examples", "block"}``. ``profile`` is ``None`` until
    Step 2. ``channel`` and ``message_type`` are accepted now but ignored — they
    exist so Step 4's scoped retrieval slots in without touching call sites.
    """
    examples = resolve_voice_examples_for_user(user, limit=limit)
    return {
        "profile": None,
        "examples": examples,
        "block": build_style_examples_block(examples),
    }
