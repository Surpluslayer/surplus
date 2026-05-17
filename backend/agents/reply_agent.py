"""
agents/reply_agent.py — Feature B: AI-contextual replies.

When a prospect replies to one of our outreach DMs, the webhook hands the
full conversation thread to `decide_reply()`. The model returns a
structured decision:

    ReplyDecision(classification, draft_text, reasoning)

The model does NOT have a `send_reply` tool. It only writes the draft +
classifies the message. The CALLER decides whether to auto-send (based
on classification + loop guard) or queue for human approval. This split
is intentional — the model can never accidentally send something it
shouldn't, because it doesn't hold the trigger.

Classifications:
    clarifying  — logistical question (when/where/who/dress code/agenda)
                  → SAFE for auto-send
    commitment  — implies a commitment, price, time guarantee, or RSVP
                  → ALWAYS queue for approval
    off_topic   — unrelated to the event
                  → ALWAYS queue
    negative    — declining, hostile, frustrated, or opt-out signal
                  → ALWAYS queue
    ambiguous   — agent can't tell
                  → ALWAYS queue
"""
from __future__ import annotations
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..jsonx import extract_json


# Classifications the orchestrator is willing to auto-send. Hard-coded
# (not env-driven) on purpose — changing this is a policy decision that
# should require a code review, not an env flip.
AUTO_SEND_CLASSES: frozenset[str] = frozenset({"clarifying"})

# All valid classifications. The agent's output is validated against this set;
# unknown classes are coerced to "ambiguous" so they queue rather than crash.
VALID_CLASSES: frozenset[str] = frozenset({
    "clarifying", "commitment", "off_topic", "negative", "ambiguous",
})

# Haiku is fast + cheap; this runs per inbound message across all users.
MODEL = os.environ.get("REPLY_AGENT_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 600


@dataclass(frozen=True)
class ThreadMessage:
    """One message in a Unipile conversation thread.

    `direction` is "outbound" if WE sent it, "inbound" if THEY sent it.
    The model needs this distinction to know whose voice is whose.
    """
    direction: str
    text: str
    ts: Optional[str] = None  # ISO-8601 string; only used in the prompt


@dataclass(frozen=True)
class ReplyDecision:
    classification: str
    draft_text: str
    reasoning: str
    raw_response: str = ""  # full model output, for debugging
    elapsed_s: float = 0.0
    error: Optional[str] = None


_SYSTEM_PROMPT = """You are an outreach assistant managing LinkedIn DMs on behalf of a human host who is inviting people to an event.

YOUR JOB
Read the conversation thread and the recipient's latest reply, then produce:
  1. A classification of the recipient's message
  2. A short draft of what should be said back
  3. Brief reasoning explaining your choice

You do not have the ability to send messages. A human (or downstream code) decides whether to send your draft. Your output is always inspected.

CLASSIFICATIONS (pick exactly one)
- "clarifying": the recipient is asking a logistical question — time, place, dress code, agenda, who else is coming, format, duration, location specifics. NO commitments involved. Safe to handle.
- "commitment": the recipient is trying to commit (RSVP yes, ask for a specific seat, request a calendar invite), or expects you to commit on the host's behalf (price, exact start time you don't know, guest list confirmation).
- "off_topic": unrelated to the event (sales pitch, recruiter spam, random question about you/the host's other work).
- "negative": declining, frustrated, opt-out signal ("not interested", "stop", "remove me"), or hostile.
- "ambiguous": you genuinely can't tell what they want or how to respond.

DRAFT GUIDELINES
- Concise. 1–3 sentences max. LinkedIn DMs are short.
- Match the recipient's register — if they wrote one line, write one line.
- NEVER invent facts. Don't make up a time, a price, an address, who else is attending, the host's bio, or specific dates. If the event facts in the user message don't cover their question, say "let me check with the host and circle back" or similar.
- NEVER apologize for the AI. Don't say "as an AI" or "I'll have someone get back to you" — write as if you were the host's assistant.
- For "negative": draft a polite acknowledgment that respects the no. No counter-pitch.
- For "commitment": draft a placeholder that defers to the host (e.g. "Glad you're interested — let me confirm the seat with the host and get back to you today."). The human will edit before sending.
- For "off_topic": draft a brief redirect or "not the right person for this, sorry."
- For "ambiguous": draft a clarifying question.

OUTPUT FORMAT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "classification": "clarifying" | "commitment" | "off_topic" | "negative" | "ambiguous",
  "draft_text": "string — the actual reply, ready to send if approved",
  "reasoning": "string — one sentence explaining your classification + draft choice"
}"""


def _format_thread(thread: list[ThreadMessage]) -> str:
    """Render the thread as a labeled conversation transcript."""
    if not thread:
        return "(no prior messages)"
    lines: list[str] = []
    for m in thread:
        label = "HOST" if m.direction == "outbound" else "RECIPIENT"
        lines.append(f"[{label}] {m.text.strip()}")
    return "\n".join(lines)


def _format_event_context(event, host) -> str:
    """Render the parts of the event the model needs to ground its replies.

    Only facts we actually have — don't fabricate placeholders for things
    we'd want the model to know (exact venue, start time, dress code). If
    a field is empty, the model should be told to defer to the host
    rather than hallucinate.
    """
    def _first(v) -> str:
        if not v:
            return ""
        if isinstance(v, list):
            return v[0] if v else ""
        return str(v).split(",")[0].strip()

    host_name = (getattr(host, "name", None) or "the host").strip()
    host_headline = getattr(host, "headline", None) or ""
    parts = [
        f"Host: {host_name}" + (f" — {host_headline}" if host_headline else ""),
        f"Event format: {event.format}",
        f"City: {event.city}",
        f"Headcount target: {event.headcount}",
        f"Primary goal: {_first(event.goal) or 'unspecified'}",
        f"ICP seniority: {_first(event.seniority) or 'unspecified'}",
        f"ICP role: {event.role}",
    ]
    return "\n".join(parts)


def _format_recipient_context(prospect) -> str:
    return (
        f"Recipient: {prospect.name}\n"
        f"Role: {prospect.role} at {prospect.company}\n"
        f"What they work on: {prospect.works_on}"
    )


def _coerce_decision(parsed: Optional[dict[str, Any]], raw: str,
                     elapsed: float, error: Optional[str] = None) -> ReplyDecision:
    """Defensively turn whatever the model returned into a ReplyDecision.

    Unknown classifications coerce to 'ambiguous' so anything we can't
    parse safely queues for approval instead of failing the webhook.
    """
    if not parsed:
        return ReplyDecision(
            classification="ambiguous",
            draft_text="",
            reasoning="(agent failed to produce parseable output — queued for review)",
            raw_response=raw, elapsed_s=elapsed, error=error or "no parseable JSON",
        )
    classification = str(parsed.get("classification") or "").strip().lower()
    if classification not in VALID_CLASSES:
        classification = "ambiguous"
    return ReplyDecision(
        classification=classification,
        draft_text=str(parsed.get("draft_text") or "").strip(),
        reasoning=str(parsed.get("reasoning") or "").strip(),
        raw_response=raw, elapsed_s=elapsed, error=error,
    )


def decide_reply(
    thread: list[ThreadMessage],
    event,
    prospect,
    host=None,
    *,
    client=None,
) -> ReplyDecision:
    """
    Single Claude call per inbound message. Synchronous on purpose — the
    webhook handler is already a short request and this keeps the loop
    trivial to reason about.

    Args:
        thread   : full conversation in chronological order
        event    : the Event ORM row
        prospect : the Prospect ORM row (recipient)
        host     : the User ORM row (the human whose LinkedIn this is); may be None
        client   : optional Anthropic client (for tests). Real path constructs
                   a fresh client per call — these are rare enough that pooling
                   isn't worth the import-time cost.
    """
    user_message = (
        "EVENT CONTEXT\n"
        f"{_format_event_context(event, host)}\n\n"
        "RECIPIENT\n"
        f"{_format_recipient_context(prospect)}\n\n"
        "CONVERSATION SO FAR\n"
        f"{_format_thread(thread)}\n\n"
        "Produce the JSON decision now."
    )

    t0 = time.time()
    try:
        if client is None:
            from anthropic import Anthropic
            client = Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[
                {"role": "user", "content": user_message},
                # Prefill assistant with "{" so Haiku is forced into JSON mode.
                {"role": "assistant", "content": "{"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return _coerce_decision(None, "", round(time.time() - t0, 2),
                                error=f"{type(exc).__name__}: {exc}")

    elapsed = round(time.time() - t0, 2)
    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full_text = "{" + "\n".join(text_chunks)
    parsed = extract_json(full_text)
    return _coerce_decision(parsed, full_text, elapsed)


def should_auto_send(decision: ReplyDecision, prior_auto_send_count: int) -> bool:
    """Single source of truth for the auto-send gate.

    Requires BOTH conditions:
      - classification is in the allow-list (currently just 'clarifying')
      - we haven't already auto-sent at least one reply in this conversation
        (loop guard, configured at 1 for v1)
    """
    if decision.classification not in AUTO_SEND_CLASSES:
        return False
    if prior_auto_send_count >= 1:
        return False
    if not decision.draft_text.strip():
        return False
    if decision.error:
        return False
    return True
