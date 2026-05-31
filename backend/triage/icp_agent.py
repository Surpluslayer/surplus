"""triage/icp_agent.py : conversational ICP builder (NL event description -> ICP).

WHY THIS EXISTS
---------------
Today an event's scoring policy starts life as a hand-authored structured ICP
(see icp_bryankim.json), which a human has to write and tune. That doesn't scale:
a host should be able to *describe their event in plain English* — "intimate
a16z fireside for technical AI founders in NYC, ~50 seats, no recruiters" — and
get a correct ICP out the other end.

This module is that front-end. It is the ONLY LLM-driven step in the ICP path:

    host free text  ──icp_agent──▶  structured ICP  ──compile_icp──▶  triage_config
       (this file)                  (dict)            (deterministic)   (engine input)

It deliberately does NOT emit a triage_config directly. It emits the same small
structured ICP dict that ``icp_compiler.compile_icp`` consumes, so the rules
layer (clamps, threshold bands, conflict resolution, auto-accept policy) stays
deterministic and testable, and the LLM is confined to the one genuinely fuzzy
job: turning intent into structured fields.

DESIGN CONTRACT
  - EVENT-AGNOSTIC: the system prompt is a generic event-curation interviewer.
    No event/sponsor/person specifics are baked in — those come from the host's
    words at call time. The matching engine stays data-driven.
  - TWO MODES, one contract:
      extract_icp(text)        -> single shot (host pasted a full description)
      run_icp_turn(messages)   -> multi-turn (interview; asks ONE question at a
                                  time until it can finalize)
    Both return an ICPAgentResult. When complete, .icp is a normalized dict ready
    for compile_icp; otherwise .question holds the next clarifying question.
  - FAIL-SAFE: no API key / malformed model output -> a result with an error and
    a minimal best-effort ICP, never an exception. The caller can always fall
    back to compile_icp({}).
  - NORMALIZED OUTPUT: whatever the model returns is filtered to the recognized
    ICP keys and type-coerced before it leaves this module, so a hallucinated
    extra field can never reach the compiler.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from ..jsonx import extract_json
from .icp_compiler import compile_icp

ICP_MODEL = os.environ.get("TRIAGE_ICP_MODEL", "claude-sonnet-4-6")
ICP_MAX_TOKENS = int(os.environ.get("TRIAGE_ICP_MAX_TOKENS", "1500"))

# The exact keys compile_icp recognizes. Anything else the model emits is dropped.
_STR_KEYS = ("role", "seniority", "co_stage", "format", "city", "goal")
_LIST_KEYS = ("priority_archetypes", "deprioritize_archetypes",
              "anti_fit", "nice_to_have")

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        _CLIENT = Anthropic(max_retries=2)
    return _CLIENT


_SYSTEM = (
    "You are an event-curation interviewer. A host is running an event and wants "
    "to curate the guest list. Your single job is to turn their description into "
    "a structured ICP (ideal-attendee profile) that a deterministic scoring "
    "engine will consume. You do NOT score anyone and you do NOT decide "
    "thresholds — you only capture intent.\n\n"
    "Be event-agnostic: work entirely from what the host tells you. Never assume "
    "a specific industry, sponsor, or seniority unless the host implies it.\n\n"
    "You must reply with ONLY a JSON object, no prose around it, in one of two "
    "shapes:\n"
    '  1. To ask for a missing essential: {"action": "ask", "question": "<one '
    'short, specific question>", "have": {<partial ICP so far>}}\n'
    '  2. When you have enough to curate confidently: {"action": "finalize", '
    '"icp": {<ICP>}, "summary": "<one sentence describing the room>"}\n\n'
    "The ICP object uses ONLY these keys (all optional, omit what you don't "
    "know):\n"
    "  role: str — the core attendee role (e.g. 'technical AI founder')\n"
    "  seniority: str — e.g. 'Founder / CTO / founding engineer'\n"
    "  co_stage: str — company stage, e.g. 'pre-seed to Series B'\n"
    "  format: str — the event format VERBATIM-ish (e.g. 'intimate fireside', "
    "'large mixer', 'invite-only dinner', 'virtual panel'); this drives how "
    "selective the engine is, so preserve words like intimate/exclusive/mixer.\n"
    "  city: str — host city if the event is in-person and location matters\n"
    "  goal: str — one sentence on what a great room looks like\n"
    "  priority_archetypes: [str] — who to BOOST. Use lowercase archetype words "
    "the engine knows: founder, investor, operator, engineer, researcher, "
    "student, executive. (Most curated rooms prioritize 'founder'.)\n"
    "  deprioritize_archetypes: [str] — who to CAP/down-weight (same vocabulary). "
    "An archetype must not appear in both lists.\n"
    "  anti_fit: [str] — concrete kinds of people who should NOT get a seat "
    "(e.g. 'recruiters prospecting for hires', 'students with no company').\n"
    "  nice_to_have: [str] — soft positive signals (e.g. 'backed by a top fund', "
    "'shipped a product with traction').\n"
    "  capacity: int — number of seats, if stated.\n"
    "  require_corroboration: bool — default true; whether a claimed identity "
    "(e.g. 'I founded X') must be externally corroborated to earn the boost.\n\n"
    "Ask a question ONLY when an ESSENTIAL is missing (you have no sense of who "
    "the room is for, or no goal). Do not interrogate the host — at most a "
    "couple of questions. If the description is already rich, finalize "
    "immediately. Prefer finalizing."
)


@dataclass
class ICPAgentResult:
    """Outcome of one agent turn."""
    complete: bool = False
    icp: Optional[dict] = None          # normalized, compile_icp-ready (when complete)
    question: str = ""                  # next clarifying question (when not complete)
    summary: str = ""                   # one-liner describing the room (when complete)
    assistant_json: str = ""            # raw model JSON (for transcript persistence)
    error: str = ""

    def to_triage_config(self) -> dict:
        """Bridge to the deterministic rules layer. Always safe to call —
        compile_icp is total over malformed/empty input."""
        return compile_icp(self.icp or {})


def _normalize_icp(raw: object) -> dict:
    """Filter the model's ICP to recognized keys + coerce types. A hallucinated
    extra field, a string where a list belongs, etc. can never reach compile_icp."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in _STR_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
        elif v not in (None, "") and not isinstance(v, (list, dict)):
            out[k] = str(v).strip()
    for k in _LIST_KEYS:
        v = raw.get(k)
        if isinstance(v, str):
            items = [v]
        elif isinstance(v, (list, tuple)):
            items = list(v)
        else:
            items = []
        cleaned = []
        seen = set()
        for it in items:
            s = (it if isinstance(it, str) else str(it)).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                cleaned.append(s)
        if cleaned:
            out[k] = cleaned
    cap = raw.get("capacity")
    if cap is not None:
        try:
            out["capacity"] = max(0, int(cap))
        except (TypeError, ValueError):
            pass
    rc = raw.get("require_corroboration")
    if isinstance(rc, bool):
        out["require_corroboration"] = rc
    elif isinstance(rc, str):
        out["require_corroboration"] = rc.strip().lower() in ("true", "yes", "1", "y")
    return out


def run_icp_turn(messages: list[dict], *, client=None) -> ICPAgentResult:
    """Run one interviewer turn over a conversation.

    `messages` is the running history as Anthropic-style dicts:
        [{"role": "user", "content": "..."}, {"role": "assistant", ...}, ...]
    Returns an ICPAgentResult: either a clarifying .question (complete=False) or a
    finalized, normalized .icp (complete=True). Never raises.
    """
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip() and client is None:
        # Degrade: we can't interview, but the caller can still compile an empty
        # ICP. Signal incomplete with an error so the UI can prompt manually.
        return ICPAgentResult(complete=False, error="ANTHROPIC_API_KEY unset")
    try:
        cli = client or _client()
        resp = cli.messages.create(
            model=ICP_MODEL,
            max_tokens=ICP_MAX_TOKENS,
            system=_SYSTEM,
            messages=messages,
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
    except Exception as exc:  # noqa: BLE001
        return ICPAgentResult(complete=False, error=f"{type(exc).__name__}: {exc}")

    data = extract_json(text)
    if not isinstance(data, dict):
        return ICPAgentResult(complete=False, assistant_json=text,
                              error="model did not return JSON")
    action = str(data.get("action") or "").lower()
    if action == "finalize":
        icp = _normalize_icp(data.get("icp"))
        return ICPAgentResult(complete=True, icp=icp,
                              summary=str(data.get("summary") or "").strip(),
                              assistant_json=text)
    # Treat anything that isn't an explicit finalize as a question turn.
    q = str(data.get("question") or "").strip()
    return ICPAgentResult(complete=False, question=q, assistant_json=text)


def extract_icp(description: str, *, client=None) -> ICPAgentResult:
    """One-shot: turn a single free-text event description into an ICP.

    Convenience over run_icp_turn for the common case where the host pastes a
    full paragraph. If the model still needs a clarification, the result will be
    incomplete with .question set — the caller can then switch to a chat loop.
    """
    return run_icp_turn(
        [{"role": "user", "content": (description or "").strip()
          or "(no description provided)"}],
        client=client)
