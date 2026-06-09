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

import hashlib
import json
import os
import re
import statistics
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


# ── list[str] → structured host_voice_profile ────────────────────────────────
# The style_examples block shows the model raw past messages and asks it to infer
# the voice every time. A *profile* does that inference ONCE, deterministically,
# and states the result as explicit style rules ("opens with 'Hey', ~20 words,
# uses emoji, exclamation-heavy"). The model then has both the distilled rules
# and the ground-truth examples, which the voice feedback flagged as the missing
# "voice packaging" layer. This builder is pure + deterministic (no LLM, no
# latency); the ``User.voice_profile`` column exists to cache a profile (or, later,
# a richer LLM-derived one) so even this cheap work is skipped on the hot path.

_GREETING_RE = re.compile(
    r"^[\s\W]*(hey there|hey|hiya|heya|hi|hello|yo|good morning|good afternoon|"
    r"good evening)\b", re.IGNORECASE)
# Closer phrases grouped by the label we surface. Order = priority on ties.
_SIGNOFFS = (
    ("thanks", ("thanks", "thank you", "thx", "many thanks", "much appreciated",
                "appreciate it", "appreciate you")),
    ("cheers", ("cheers",)),
    ("talk soon", ("talk soon", "speak soon", "chat soon", "ttyl", "more soon",
                   "catch up soon", "let's catch up")),
    ("looking forward", ("looking forward", "look forward")),
    ("best", ("best regards", "all the best", "warm regards", "kind regards",
              "regards", "warmly", "best,", "best!")),
)
# Common emoji ranges (symbols/pictographs, dingbats, flags, hearts, sparkles).
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "❤✨✅⭐✌✋✊]")


def _word_count(s: str) -> int:
    return len(re.findall(r"\b[\w']+\b", s or ""))


def fingerprint_examples(examples: list[str]) -> str:
    """Stable short hash of the examples a profile was built from, so a cached
    ``User.voice_profile`` can be matched to (and invalidated by) the current
    examples. Order-sensitive on purpose: the example list is itself capped and
    ordered, so a reorder is a real change."""
    h = hashlib.sha256()
    for ex in examples or []:
        h.update((ex or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def build_host_voice_profile(examples: list[str]) -> Optional[dict]:
    """Distil a host's past messages into explicit, deterministic style rules.

    Returns ``None`` when there are no examples (the caller then renders nothing
    and the surface falls back to generic-but-warm). Every field is computed from
    the example strings alone — no model call — so this is safe to run inline."""
    examples = [e.strip() for e in (examples or []) if e and e.strip()]
    if not examples:
        return None

    lengths = [_word_count(e) for e in examples]
    avg = round(statistics.mean(lengths)) if lengths else 0
    length_band = "short" if avg < 25 else ("medium" if avg <= 55 else "long")

    # Greeting: the most common opener form across examples (else None).
    greetings = [m.group(1).lower()
                 for m in (_GREETING_RE.match(e) for e in examples) if m]
    greeting = _most_common(greetings)
    opens_lowercase = (sum(1 for e in examples if e[:1].islower())
                       / len(examples)) >= 0.5

    # Sign-off: scan the tail of each message for a known closer.
    signoff_hits: list[str] = []
    for e in examples:
        tail = e[-40:].lower()
        for label, phrases in _SIGNOFFS:
            if any(p in tail for p in phrases):
                signoff_hits.append(label)
                break
    signoff = _most_common(signoff_hits)

    uses_emoji = any(_EMOJI_RE.search(e) for e in examples)
    emoji_samples: list[str] = []
    for e in examples:
        for ch in _EMOJI_RE.findall(e):
            if ch not in emoji_samples:
                emoji_samples.append(ch)
            if len(emoji_samples) >= 3:
                break
        if len(emoji_samples) >= 3:
            break

    exclamations = sum(e.count("!") for e in examples)
    excl_per_msg = exclamations / len(examples)
    exclamation = ("frequent" if excl_per_msg >= 0.8
                   else ("occasional" if excl_per_msg >= 0.2 else "rare"))

    casual_signals = bool(greeting in ("hey", "hey there", "yo", "heya", "hiya")
                          or opens_lowercase or uses_emoji
                          or exclamation == "frequent")
    formal_signals = bool(signoff in ("best", "looking forward")
                          and not casual_signals)
    formality = "casual" if casual_signals else ("formal" if formal_signals else "neutral")

    return {
        "n_examples": len(examples),
        "avg_words": avg,
        "length_band": length_band,
        "greeting": greeting,
        "opens_lowercase": opens_lowercase,
        "signoff": signoff,
        "uses_emoji": uses_emoji,
        "emoji_samples": emoji_samples,
        "exclamation": exclamation,
        "formality": formality,
    }


def _most_common(items: list[str]) -> Optional[str]:
    if not items:
        return None
    # max by count, ties broken by first appearance (stable) for determinism.
    return max(dict.fromkeys(items), key=items.count)


def render_voice_profile_block(profile: Optional[dict]) -> str:
    """Render a ``host_voice_profile`` as a ``<host_voice_profile>`` instruction
    block, or ``""`` when there's no profile. The block states the distilled
    rules as defaults and explicitly defers to the style_examples as ground
    truth, so the two layers never contradict each other in the model's eyes."""
    if not profile:
        return ""
    lines = ["", "<host_voice_profile>",
             "Distilled style rules from the host's own past messages. Follow "
             "these as defaults; the style_examples below are the ground truth "
             "if they ever disagree."]

    lines.append(f"- Typical length: ~{profile['avg_words']} words "
                 f"({profile['length_band']}). Stay close to this.")

    if profile.get("greeting"):
        lines.append(f"- Greeting: usually opens with \"{profile['greeting'].title()}\" "
                     "(use the recipient's first name if natural).")
    elif profile.get("opens_lowercase"):
        lines.append("- Greeting: often starts lowercase / no formal greeting.")

    if profile.get("signoff"):
        lines.append(f"- Sign-off: tends to close with a \"{profile['signoff']}\"-style line.")

    if profile.get("uses_emoji"):
        ex = " ".join(profile.get("emoji_samples") or [])
        lines.append(f"- Emoji: uses emoji sometimes{f' (e.g. {ex})' if ex else ''}; "
                     "match the rate, do not overdo it.")
    else:
        lines.append("- Emoji: does not use emoji.")

    excl = profile.get("exclamation")
    if excl == "frequent":
        lines.append("- Punctuation: warm and exclamatory, uses exclamation points freely.")
    elif excl == "rare":
        lines.append("- Punctuation: measured, sparing with exclamation points.")

    lines.append(f"- Overall tone: {profile['formality']}.")
    lines.append("</host_voice_profile>")
    return "\n".join(lines)


def resolve_voice_profile_for_user(user: Any, examples: list[str]) -> Optional[dict]:
    """Return the host's voice profile for the given (already-resolved) examples.

    Prefers a cached profile on ``User.voice_profile`` when its stored
    fingerprint matches the current examples (the seam for a future out-of-band /
    LLM-built profile); otherwise builds the deterministic profile inline. Any
    attribute/JSON failure falls through to the inline build, so a stale or
    malformed cache can never break a draft."""
    fp = fingerprint_examples(examples)
    try:
        raw = (getattr(user, "voice_profile", "") or "").strip() if user is not None else ""
    except Exception:  # noqa: BLE001 - DetachedInstanceError + friends
        raw = ""
    if raw:
        try:
            cached = json.loads(raw)
            if isinstance(cached, dict) and cached.get("fingerprint") == fp:
                prof = cached.get("profile")
                if isinstance(prof, dict):
                    return prof
        except (json.JSONDecodeError, TypeError):
            pass
    return build_host_voice_profile(examples)


# ── The seam both surfaces converge on ───────────────────────────────────────

def build_voice_context(user: Any, *, channel: Optional[str] = None,
                        message_type: Optional[str] = None,
                        limit: int = 8) -> dict:
    """Resolve everything a draft call needs to speak in the host's voice.

    Returns ``{"profile", "examples", "block"}`` where ``block`` is the
    model-ready voice context: the ``<host_voice_profile>`` rules (if any)
    followed by the ``<style_examples>`` ground-truth messages. ``channel`` and
    ``message_type`` are accepted now but ignored — they exist so Step 4's scoped
    retrieval slots in without touching call sites.
    """
    examples = resolve_voice_examples_for_user(user, limit=limit)
    profile = resolve_voice_profile_for_user(user, examples)
    block = render_voice_profile_block(profile) + build_style_examples_block(examples)
    return {
        "profile": profile,
        "examples": examples,
        "block": block,
    }
