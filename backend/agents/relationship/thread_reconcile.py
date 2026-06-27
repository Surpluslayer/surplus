"""Reconcile durable spine fields (capture next_step, etc.) with the live thread.

Capture metadata is written once at scan time and never auto-cleared. Message
history is the moving source of truth. Before triage/composer/agent briefs
assert an open loop, check whether prior outbound messages already addressed it.
"""
from __future__ import annotations

import re

# Host lines that mean the obligation was carried out (not merely promised).
_DELIVERY_PHRASES = (
    "here's", "here is", "attached", "as promised", "as discussed",
    "following up with", "sent you", "sent over", "sent the", "sending you",
    "sending the", "got it over", "link below", "see below", "per our chat",
    "per our conversation", "just sent", "dropped in your inbox",
)

# Forward-looking only — token overlap on these must NOT close an obligation.
_PROMISE_PHRASES = (
    "i'll", "i will", "ill ", "let me", "lemme", "will send", "will share",
    "will intro", "will follow", "happy to send", "plan to send",
)

_STOP = frozenset({
    "the", "and", "for", "with", "you", "your", "this", "that", "from",
    "have", "will", "send", "over", "about", "just", "when", "next", "week",
    "time", "grab", "quick", "short", "follow", "followup", "follow-up",
})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def obligation_tokens(obligation: str) -> list[str]:
    """Meaningful tokens from a next_step / obligation string."""
    words = re.findall(r"[a-z0-9]+", _norm(obligation))
    out: list[str] = []
    for w in words:
        if len(w) < 3 or w in _STOP:
            continue
        if w not in out:
            out.append(w)
    return out[:8]


def _url_bits(obligation: str) -> list[str]:
    bits: list[str] = []
    for part in re.split(r"\s+", obligation.strip()):
        p = part.strip(".,);")
        if "." in p and len(p) >= 6:
            bits.append(p.lower())
    return bits


def message_addresses_obligation(message: str, obligation: str) -> bool:
    """True when `message` looks like the host already acted on `obligation`."""
    m = _norm(message)
    o = _norm(obligation)
    if not m or not o:
        return False

    # next_step woven into the first outbound note (common at capture).
    if len(o) >= 6 and o in m:
        return True

    for bit in _url_bits(obligation):
        if bit in m:
            return True

    tokens = obligation_tokens(o)
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in m)
    if hits < max(1, (len(tokens) + 1) // 2):
        return False

    if any(p in m for p in _DELIVERY_PHRASES):
        return True

    # Promise-only wording with no delivery cue — still open.
    if any(p in m for p in _PROMISE_PHRASES):
        return False

    return False


def obligation_still_open(obligation: str | None, prior: list[dict]) -> bool:
    """False when outbound thread history shows the obligation was handled."""
    step = (obligation or "").strip()
    if not step:
        return False
    host_msgs = [m for m in (prior or []) if m.get("who") == "host"]
    if not host_msgs:
        return True
    return not any(
        message_addresses_obligation(m.get("text") or "", step)
        for m in host_msgs
    )


def reconcile_next_step(next_step: str | None, prior: list[dict]) -> str | None:
    """Return next_step when still open, else None (for prompts / roster)."""
    step = (next_step or "").strip()
    if not step:
        return None
    return step if obligation_still_open(step, prior) else None


def apply_to_facts(facts: dict, prior: list[dict]) -> dict:
    """Drop stale capture next_step from composer/agent fact bundles."""
    out = dict(facts or {})
    if out.get("next_step") and not obligation_still_open(out["next_step"], prior):
        out["next_step"] = None
    return out


# ── Thread window (rolling compression) ──────────────────────────────────────
# Keep recent messages verbatim; collapse older exchanges into one line so
# prompts stay focused without losing that a conversation happened.

DEFAULT_RECENT_MESSAGES = 5


def _clip(text: str, n: int = 72) -> str:
    t = _norm(text)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def summarize_older_thread(older: list[dict]) -> str:
    """Deterministic one-line rollup of messages before the recent window."""
    bits: list[str] = []
    for m in older or []:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = m.get("who")
        label = "You" if who == "host" else ("Them" if who == "them" else "Note")
        bits.append(f"{label}: {_clip(text)}")
    if not bits:
        return ""
    head = bits[:6]
    tail = f" (+{len(bits) - 6} earlier)" if len(bits) > 6 else ""
    return "Earlier conversation: " + " · ".join(head) + tail


def window_thread(
    prior: list[dict],
    recent: int = DEFAULT_RECENT_MESSAGES,
) -> tuple[list[dict], str | None]:
    """Return (recent verbatim messages, optional summary of older ones)."""
    msgs = list(prior or [])
    limit = max(1, recent)
    if len(msgs) <= limit:
        return msgs, None
    older, recent_msgs = msgs[:-limit], msgs[-limit:]
    summary = summarize_older_thread(older)
    return recent_msgs, summary or None


def clear_prospect_next_step_if_fulfilled(
    prospect,
    message_text: str,
) -> bool:
    """Clear capture next_step on send when the outbound body addressed it."""
    step = (getattr(prospect, "next_step", None) or "").strip()
    if not step:
        return False
    if message_addresses_obligation(message_text or "", step):
        prospect.next_step = None
        return True
    return False
