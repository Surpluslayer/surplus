"""
agents/relationship_agent.py : the first genuinely agentic surface.

Where reply_agent / outreach are single-shot ("here's a thread, write a
reply"), this is a *loop* : the model surveys your relationship spine, picks
who needs attention, pulls each person's history, and proposes a concrete
next move — looping tool-by-tool until it's worked the list or hits the step
cap. It runs on agent_loop.run_agent (the bounded tool-use primitive).

SAFETY — propose-only by construction:
  The agent has NO tool that sends a message or writes to the database. Its
  "act" tools (`propose_next_step`, `draft_message`) only stage suggestions
  into an in-memory bag that we hand back to the caller. Nothing leaves the
  process without a human approving it downstream. This mirrors the
  reply_agent split (the model never holds the trigger) and means the worst
  case of a hallucinating loop is a bad *suggestion*, never a bad send.

  Graduating to "act with guardrails" later is purely additive : swap a
  propose tool's impl to call add_note / send_and_log behind a policy gate.
  The loop, the prompt, and the read tools don't change.

The read tools wrap the deterministic contact spine (relationships.py), so
the agent reasons over the SAME auditable facts the CRM page shows : it can't
invent contacts, stages, or timelines.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import relationships
from . import voice
from .agent_loop import DEFAULT_MODEL, _block_type, run_agent
from ..providers.base import strip_em_dashes

# How many contacts the agent may pull full history for in one run. A soft
# guard on cost/latency : the survey tool returns everyone, but deep-diving
# all of them would be wasteful. The model is told to prioritise.
# Raised to 100 for now : the concurrent fan-out (load-tested at 100 drafts on
# Railway, 0 errors, ~30s) makes a large batch cheap in wall-time. Keep the
# triage token budget (_TRIAGE_MAX_TOKENS) large enough to actually emit this
# many ranked selections in one call.
MAX_DEEP_DIVES = 100


_SYSTEM_PROMPT = (
    "You are a relationship manager for an event host. You work their durable "
    "contact spine — the people they've met across events — and keep those "
    "relationships from going cold.\n\n"
    "Your loop each run:\n"
    "1. The host's FULL contact roster is given to you inline (one row per "
    "person, with the same signals a survey would return). You already have it "
    "— do NOT waste a turn fetching it; read the roster directly.\n"
    "2. Decide WHO to follow up with from these signals (NOT from fresh "
    "external news), in this PRIORITY ORDER:\n"
    "   a. MARKED follow-ups — AUTHORITATIVE: `marked_follow_up` true (the host "
    "tagged them 'follow_up') OR a `next_step` the host wrote down. The host has "
    "EXPLICITLY decided to follow up with these people, so they are ALWAYS worth "
    "actioning this run. CRITICAL: an initial outreach that hasn't been answered "
    "yet is the REASON to follow up, never a reason to wait. Do NOT skip a marked "
    "contact just because their first message is recent or still unanswered — "
    "that recency/no-reply logic does NOT apply to marked contacts. Suppress a "
    "marked contact ONLY if the host has already acted AGAIN: `prior_messages` "
    "shows a SECOND host message after the first (a real follow-up already went "
    "out) OR the contact has REPLIED (an inbound 'them' message). Absent those, "
    "draft the follow-up.\n"
    "   b. Conversation context: after `get_contact`, read `prior_messages`. An "
    "unanswered question or open loop from THEM (the last message is theirs and "
    "expects a reply) is a high-priority candidate; a thread the host left "
    "mid-conversation is a candidate too.\n"
    "   c. Stale, UNMARKED: a live relationship gone quiet past the stale line "
    "is a candidate. BUT for an unmarked contact whose ONLY touch is a recent, "
    "un-replied first outreach, hold off — a second message this soon would be "
    "piling on. Note them as 'revisit if it goes stale' rather than drafting now. "
    "This hold-off applies ONLY to unmarked contacts; it never overrides (a).\n"
    "3. For each candidate, call `get_contact` to read their full "
    "history (events shared, stage, timeline) BEFORE deciding anything. Never "
    "propose a move without reading the history first. Pay special attention to "
    "`prior_messages` — the actual thread between the host and this person. The "
    "FIRST item there is the initial message/context (the first DM or the note "
    "from when they met); a follow-up must read as a continuation of THAT "
    "conversation, not a fresh cold open.\n"
    "4. Propose ONE concrete move per person you act on: either "
    "`propose_next_step` (a specific action the host should take) and/or "
    "`draft_message` (a short, warm, specific message). The draft MUST build on "
    "`prior_messages`: pick up the thread where it left off, reference what was "
    "already said or the initial context, and only then add the new reason to "
    "reach out (a job change, a post, time passing). Never generic, never a "
    "restart that ignores the first message. Quality over quantity.\n"
    "   WORK ONE PERSON AT A TIME: read a candidate with `get_contact`, then "
    "immediately propose their move before moving to the next person. Do NOT "
    "batch-read everyone first — the host watches each proposal appear live, so "
    "interleaving read->propose gets the first one in front of them fastest.\n"
    "5. When you've worked the priority list, stop and give a SHORT, "
    "conversational closing line, like you're texting the host back. ONE or two "
    "sentences, plain prose. NEVER use markdown tables, headers (#), or bullet "
    "lists — the per-person detail already rides along in each draft's "
    "`rationale`, so the summary is just a friendly wrap-up (e.g. \"Drafted "
    "first-touches for all 5, Shama's the one I'd prioritize.\"). Do not "
    "re-list everyone you already drafted.\n\n"
    "Rules: Only use facts returned by the tools — never invent an event, a "
    "name, or a detail. You CANNOT send anything; you only propose. Keep "
    "drafts under ~60 words and human, not salesy. NEVER use em dashes (—) or "
    "en dashes (–) in a draft — they read as AI-written; use a comma, a period, "
    "or restructure the sentence instead. If a <style_examples> block is "
    "provided below, every draft MUST be written in the host's voice as shown "
    "there (their greeting, sign-off, sentence length, formality, punctuation "
    "and emoji habits) — match the voice, not the content. This is the SAME "
    "voice the host's first message was written in; a follow-up must sound like "
    "the same person."
)


def _host_voice_examples(db, user_id: int) -> list[str]:
    """Resolve the host's voice-matching examples, same source + order the
    initial-message composer uses (agents/outreach._get_voice_examples), so a
    follow-up sounds like the SAME person who sent the first DM:

      1. User.voice_examples (JSON list, auto-synced from real LinkedIn sent
         messages via live_enrich.sync_host_voice, or set via the admin endpoint)
      2. OPERATOR_VOICE_EXAMPLES env var (JSON list) — legacy fallback
      3. [] — no style guide, drafts fall back to generic-but-warm

    Bad JSON anywhere is treated as empty so a typo can't break a run. Capped
    at 8 to keep input tokens bounded (matches the composer's cap).

    Thin wrapper over the shared voice layer (agents/voice.py) so the cold-DM
    composer and this follow-up agent resolve voice identically."""
    from .. import models
    try:
        user = db.get(models.User, user_id)
    except Exception:  # noqa: BLE001 - keep the run alive on any lookup failure
        user = None
    return voice.resolve_voice_examples_for_user(user)


def _voice_block(examples: list[str]) -> str:
    """Format the host's past messages as a style primer, mirroring the
    composer's <style_examples> block ('match their voice, not the content')
    so the two surfaces speak in one consistent voice. Delegates to the shared
    voice layer (agents/voice.py)."""
    return voice.build_style_examples_block(examples)


def _voice_context_block(db, user_id: int) -> str:
    """Full model-ready voice context for this host via the shared voice layer:
    the distilled <host_voice_profile> rules followed by the ground-truth
    <style_examples>. Used by both draft paths so each Sonnet draft gets the
    packaged voice, not just raw examples. DetachedInstance/lookup-safe."""
    from .. import models
    try:
        user = db.get(models.User, user_id)
    except Exception:  # noqa: BLE001 - keep the run alive on any lookup failure
        user = None
    return voice.build_voice_context(
        user, channel="linkedin", message_type="warm_followup")["block"]


def _strip_dashes(text: str) -> str:
    """Belt-and-suspenders on the prompt's no-em-dash rule: rewrite any dash the
    model slips into a staged draft to a comma, so the AI 'tell' never leaks.

    Delegates to the canonical ``providers.base.strip_em_dashes`` so this surface
    scrubs IDENTICALLY to the cold-DM composer. That scrubber is strictly more
    thorough than the em/en-only regex this used to carry: it also catches the
    figure dash (―), the unicode minus (−), and a spaced ASCII hyphen used as a
    dash ('Vancouver - let's chat') — all of which previously leaked here. A
    hyphen inside a word ('co-founder') is still left untouched."""
    return strip_em_dashes(text or "") or ""


def _oxford_join(names: list[str]) -> str:
    """'A' | 'A and B' | 'A, B, and C' — for human-readable name lists."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _reconcile_summary(drafted: list[str], held: list[dict]) -> str:
    """Truthful wrap-up built from ACTUAL outcomes, not the triage's pre-draft
    promise: name who got drafted, and who was held off (and why, when there's a
    single clear reason). Used only when a nominee was skipped, so the summary
    can never claim a draft the host won't actually see on screen."""
    if drafted:
        lead = f"Drafted follow-ups for {_oxford_join(drafted)}."
    else:
        lead = "Didn't draft anyone this run."
    held_names = _oxford_join([h["name"] for h in held])
    reason = (held[0].get("reason") or "").strip() if len(held) == 1 else ""
    if reason:
        reason = reason[0].lower() + reason[1:]  # de-cap first char only
        tail = f"Held off on {held_names} since {reason.rstrip('.')}."
    else:
        tail = (f"Held off on {held_names} since the threads don't show a clear "
                "reason to nudge yet.")
    return f"{lead} {tail}"


_TOOLS = [
    {
        "name": "get_contact",
        "description": (
            "Read one person's full history before deciding: rollup summary, "
            "the events you've shared, the cross-event timeline of every touch, "
            "and `prior_messages` — the distilled host<->contact message thread "
            "(first DM, capture note, replies), oldest-first. Ground any draft "
            "in that thread. Always call this before proposing a move."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer",
                               "description": "The contact_id from the roster."},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "propose_next_step",
        "description": (
            "Propose a concrete next action the host should take with this "
            "person (e.g. 'intro them to Priya from the Seed dinner'). Staged "
            "for the host to approve — this does NOT take the action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "next_step": {"type": "string",
                              "description": "The specific action, one sentence."},
                "rationale": {"type": "string",
                              "description": "Why now / why this, grounded in history."},
            },
            "required": ["contact_id", "next_step"],
        },
    },
    {
        "name": "draft_message",
        "description": (
            "Draft a short, warm follow-up that CONTINUES the existing thread "
            "in `prior_messages` (build on the initial message / what was "
            "already said), then adds the new reason to reach out. Grounded in "
            "real shared history, never a generic cold restart. Staged for the "
            "host to review and send — this does NOT send anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "message": {"type": "string",
                            "description": "The message, under ~60 words."},
                "rationale": {"type": "string",
                              "description": "ONE short, friendly sentence on why "
                              "this person / why now, grounded in history. Reads "
                              "as a chat aside to the host, not a report."},
            },
            "required": ["contact_id", "message"],
        },
    },
]


@dataclass
class Proposal:
    """One staged suggestion the agent produced. Nothing here has happened
    yet : it's a recommendation awaiting human approval."""
    kind: str            # "next_step" | "draft_message"
    contact_id: int
    contact_name: str
    text: str            # the next_step or the message body
    rationale: str = ""


@dataclass
class RelationshipAgentResult:
    """Outcome of one propose-only relationship-agent run."""
    proposals: list[Proposal] = field(default_factory=list)
    summary: str = ""
    contacts_seen: int = 0
    steps: int = 0
    stop_reason: str = ""
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "contacts_seen": self.contacts_seen,
            "steps": self.steps,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "proposals": [
                {"kind": p.kind, "contact_id": p.contact_id,
                 "contact_name": p.contact_name, "text": p.text,
                 "rationale": p.rationale}
                for p in self.proposals
            ],
        }


# Timeline source_types that carry actual host<->contact conversation (the
# thread a follow-up should continue), in contrast to derived/system rows
# (conversion, next_step, profile updates).
_MESSAGE_SOURCE_TYPES = {"in_person_capture", "manual_note", "linkedin_outreach",
                         "email"}


def _thread_from_timeline(timeline: list[dict]) -> list[dict]:
    """Distil the message/note thread out of a contact's full timeline so the
    agent can ground a follow-up in what was actually said. Keeps only the
    conversational rows (capture note, sent/received DMs, manual notes, email),
    each as {when, who, channel, text}, oldest-first. The first item is the
    'initial message' the host wants every follow-up to build on."""
    thread: list[dict] = []
    for it in timeline or []:
        if it.get("source_type") not in _MESSAGE_SOURCE_TYPES:
            continue
        # Operator-only items (the private_note, stored as a private manual_note)
        # must never reach the draft context — mirror relationship_context()'s
        # private filter so a private memo can't shape an outbound draft.
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
            "who": who,                       # host | them | context
            "channel": it.get("channel") or it.get("source_type"),
            "text": text[:600],
        })
    return thread


def _days_since(dt: Any) -> Optional[int]:
    aware = relationships._as_aware(dt)
    if aware is None:
        return None
    return (datetime.now(timezone.utc) - aware).days


# ── Thread-derived recall signals ────────────────────────────────────────────
# Phase-1 triage reasons over the structured roster row, NOT the message text,
# so any follow-up reason that lives only inside the thread (e.g. the host wrote
# "I'll send the deck next week" and never did) is invisible to triage and the
# person gets dropped as "recent unanswered outreach" before Phase-2 ever sees
# them. That is the "misses obvious people" bug: Phase-1 controls recall, and a
# message-blind Phase-1 can't recall a content-only open loop.
#
# Fix: distil cheap recall signals from the thread at roster-build time and put
# them ON the row. The structural ones (who spoke last, how long ago) are exact;
# the semantic ones (a host promise, an unanswered question) are LOOSE keyword
# heuristics ON PURPOSE — they feed recall, and Phase-2 reads the real thread and
# is the precision filter, so a false "open loop" costs only one skipped draft
# while a missed one is the bug we're fixing. No LLM call, so triage stays cheap.

# Forward-looking commitments in a host message ("I'll send...", "let me intro
# you", "circle back next week"). Loose on purpose; Phase-2 confirms.
_PROMISE_PHRASES = (
    "i'll", "i will", "ill ", "i`ll", "i´ll", "let me", "lemme", "send you",
    "send over", "send through", "send that", "shoot you", "shoot over",
    "get you", "get back to you", "follow up", "followup", "circle back",
    "loop you", "loop back", "ping you", "reach back", "intro you",
    "introduce you", "connect you", "happy to send", "will send", "will follow",
    "will get", "will share", "will intro", "will connect", "once i", "i'll grab",
    "i'll set", "i'll find", "i'll check", "i'll dig", "let you know",
)
_INTRO_WORDS = ("intro", "introduce", "introduction", "connect you")
_RESOURCE_WORDS = ("deck", "link", "doc", "resource", "slide", "pdf", "notion",
                   "guide", "template", "memo", "write-up", "writeup", "send",
                   "share", "report")
_SCHEDULE_WORDS = ("schedule", "calendar", "meet", "call", "coffee", "grab time",
                   "book", "slot", "sync", "catch up", "find time", "set up time")
_CHECKBACK_WORDS = ("check back", "circle back", "follow up", "followup",
                    "touch base", "down the line", "next week", "next month",
                    "later", "reconnect")


def _classify_open_loop(low: str) -> str:
    """Best-guess the KIND of open loop from a lowercased message, for the
    `open_loop_type` enum. Order matters: a more specific kind wins."""
    if any(w in low for w in _INTRO_WORDS):
        return "intro"
    if any(w in low for w in _SCHEDULE_WORDS):
        return "schedule"
    if any(w in low for w in _RESOURCE_WORDS):
        return "send_resource"
    if any(w in low for w in _CHECKBACK_WORDS):
        return "check_back"
    return "other"


def _thread_signals(thread: list[dict]) -> dict:
    """Cheap recall signals derived from a contact's message thread (the list
    `_thread_from_timeline` returns), so message-blind Phase-1 triage can spot
    content-level open loops. Structural fields are exact; the promise/question
    fields are loose heuristics (Phase-2 is the precision backstop). Pure
    function over the thread, so it's directly unit-testable."""
    msgs = [m for m in (thread or []) if m.get("who") in ("host", "them")]
    sig = {
        "last_message_from": None,        # "host" | "contact" | None
        "last_message_age_days": None,
        "awaiting_host_reply": False,     # contact spoke last, host owes a reply
        "awaiting_contact_reply": False,  # host spoke last, waiting on them
        "host_open_promise": False,       # host committed to a next move, undone
        "contact_open_question": False,   # contact asked something, unanswered
        "open_loop_detected": False,
        "open_loop_type": None,           # send_resource|schedule|intro|answer_question|check_back|other
        "open_loop_evidence": None,
        "followup_due": False,
    }
    if not msgs:
        return sig

    last = msgs[-1]
    who = "host" if last.get("who") == "host" else "contact"
    age = _days_since(last.get("when"))
    text = (last.get("text") or "").strip()
    low = text.lower()

    sig["last_message_from"] = who
    sig["last_message_age_days"] = age
    sig["awaiting_host_reply"] = (who == "contact")
    sig["awaiting_contact_reply"] = (who == "host")

    if who == "contact" and "?" in text:
        sig["contact_open_question"] = True
        sig["open_loop_type"] = "answer_question"
        sig["open_loop_evidence"] = text[:160]
    elif who == "host" and any(p in low for p in _PROMISE_PHRASES):
        sig["host_open_promise"] = True
        sig["open_loop_type"] = _classify_open_loop(low)
        sig["open_loop_evidence"] = text[:160]

    sig["open_loop_detected"] = bool(sig["contact_open_question"]
                                     or sig["host_open_promise"])
    # "Due" = a real obligation exists AND it isn't brand-new (>= 1 day), so a
    # same-moment exchange isn't flagged overdue. Phase-2 owns precise timing.
    has_obligation = sig["open_loop_detected"] or sig["awaiting_host_reply"]
    sig["followup_due"] = bool(has_obligation and (age or 0) >= 1)
    return sig


# ── Phase-2 context brief (deterministic, no LLM call) ────────────────────────
# Phase-2 currently hands the drafting model a one-line triage reason plus the
# raw context JSON and asks it, every time, to (a) reconstruct the thread
# situation and (b) decide what is safe to assert. That reconstruction is where
# grounding and voice slip: the model re-guesses the state of play and sometimes
# treats the LOOSE triage angle as established fact, then writes from it.
#
# The brief is a small, PRE-DIGESTED read of the situation built from the same
# thread signals Phase-1 used plus the rollup summary. It is purely deterministic
# (no extra Anthropic call, so it adds no latency) and it is NOT authoritative
# over the actual conversation: the heuristics that feed it (open_loop_type, the
# promise/question keyword match) are best-guesses, so the drafting prompt tells
# the model that prior_messages wins on any conflict. Its value is to let the
# prompt lead with "here is the situation, here is what is safe to say, here is
# what to treat as uncertain" instead of making the model derive all of that
# from scratch under a 1024-token budget.

def _last_text(thread: list[dict], who: Optional[str] = None) -> Optional[str]:
    """The most recent message text in `thread`, optionally restricted to a
    speaker ('host' | 'them'). Trimmed for prompt economy; None when absent."""
    for m in reversed(thread or []):
        if who is not None and m.get("who") != who:
            continue
        t = (m.get("text") or "").strip()
        if t:
            return t[:200]
    return None


def _context_brief(sel: dict, ctx: dict) -> dict:
    """Deterministic situation brief for the Phase-2 drafting model.

    Pure function of the triage selection (`sel`: contact_id/reason/angle) and
    the materialised context (`ctx`: summary/events/timeline/prior_messages), so
    it is directly unit-testable and adds no latency. Every field is derived from
    data already in `ctx`; nothing here overrides the real thread.

    Fields:
      - relationship_state: stage + staleness + days since last touch
      - thread_status: who spoke last and who owes the next move
      - natural_action: the single most likely next action, from the signals
      - safe_facts_to_use: facts actually present in the data (name/company/
        shared events / host's noted next step / quoted open-loop evidence)
      - facts_to_avoid_or_treat_as_uncertain: the heuristic angle + the classes
        of fact the model must not invent
      - desired_next_step: the host's own written next_step, if any
      - continue_from: the last message in the thread, to continue from
      - do_not_repeat: the host's most recent message, so the draft doesn't
        restate what was already said
      - drafting_risks: deterministic skip/caution flags (too soon, their court,
        no prior thread)
    """
    summary = ctx.get("summary") or {}
    thread = ctx.get("prior_messages") or []
    events = ctx.get("events") or []
    sig = _thread_signals(thread)

    # contact register: how formally THEY write, so the draft can meet their
    # register while keeping the host's identity (deterministic, model-free).
    contact_texts = [m.get("text") or "" for m in thread if m.get("who") == "them"]
    contact_register = voice.detect_register(contact_texts)

    name = (summary.get("name") or "").strip()
    company = (summary.get("company") or "").strip()
    stage = summary.get("relationship_stage")
    next_step = (summary.get("next_step") or "").strip()
    days = _days_since(summary.get("last_touch_at"))
    is_stale = bool(stage == "stale")
    age = sig["last_message_age_days"]

    # relationship_state -------------------------------------------------------
    rel_bits = []
    if stage:
        rel_bits.append(str(stage))
    if is_stale and stage != "stale":
        rel_bits.append("stale")
    if days is not None:
        rel_bits.append(f"{days}d since last touch")
    relationship_state = ", ".join(rel_bits) or "unknown"

    # thread_status ------------------------------------------------------------
    if not thread:
        thread_status = "no prior messages on file"
    elif sig["awaiting_host_reply"]:
        thread_status = (f"contact spoke last ({age}d ago); the host owes a reply"
                         if age is not None else
                         "contact spoke last; the host owes a reply")
    elif sig["awaiting_contact_reply"]:
        thread_status = (f"host spoke last ({age}d ago); waiting on the contact"
                         if age is not None else
                         "host spoke last; waiting on the contact")
    else:
        thread_status = "thread present"

    # natural_action -----------------------------------------------------------
    if next_step:
        natural_action = f"act on the host's written next step: {next_step}"
    elif sig["contact_open_question"]:
        natural_action = "answer the question the contact asked"
    elif sig["host_open_promise"]:
        kind = sig["open_loop_type"] or "what was promised"
        natural_action = f"deliver on the host's open promise ({kind})"
    elif sig["awaiting_host_reply"]:
        natural_action = "respond to the contact's most recent message"
    elif is_stale:
        natural_action = "reconnect after time has passed, only if a natural angle exists"
    else:
        natural_action = (sel.get("angle") or "").strip() or \
            "decide from the thread whether any touch is warranted"

    # safe_facts_to_use --------------------------------------------------------
    safe: list[str] = []
    if name:
        safe.append(f"name: {name}")
    if company:
        safe.append(f"company: {company}")
    if events:
        labels = [str(e.get("name") or e.get("title") or "").strip()
                  for e in events if isinstance(e, dict)]
        labels = [l for l in labels if l]
        if labels:
            safe.append("shared events: " + ", ".join(labels[:4]))
        else:
            safe.append(f"{len(events)} shared event(s)")
    if next_step:
        safe.append(f"host's noted next step: {next_step}")
    if sig["open_loop_evidence"]:
        safe.append("open-loop evidence (verify against prior_messages): "
                    + sig["open_loop_evidence"])

    # facts_to_avoid_or_treat_as_uncertain -------------------------------------
    avoid = [
        "any life, work, company, funding, job-change, move, launch, or milestone "
        "update that is NOT visible in prior_messages",
        "warmth, interest, or intent the thread does not actually show",
    ]
    angle = (sel.get("angle") or "").strip()
    if angle:
        avoid.append(f"the triage angle is a hint to verify, not a confirmed fact: {angle}")
    if sig["open_loop_type"] and sig["host_open_promise"]:
        avoid.append(f"open_loop_type ('{sig['open_loop_type']}') is a keyword guess; "
                     "confirm the actual promise in prior_messages")

    # drafting_risks -----------------------------------------------------------
    risks: list[str] = []
    if not thread:
        risks.append("no prior thread on file; do not fabricate shared history")
    if sig["awaiting_contact_reply"]:
        risks.append("the contact has the next natural move; the host may owe nothing")
        if (age or 0) < 3:
            risks.append(f"host messaged recently ({age}d ago) with no reply yet; "
                         "likely TOO SOON or THEIR COURT, consider skip")

    return {
        "relationship_state": relationship_state,
        "thread_status": thread_status,
        "natural_action": natural_action,
        "safe_facts_to_use": safe,
        "facts_to_avoid_or_treat_as_uncertain": avoid,
        "desired_next_step": next_step or None,
        "continue_from": _last_text(thread),
        "do_not_repeat": _last_text(thread, who="host"),
        "drafting_risks": risks,
        "contact_register": contact_register,
        "register_guidance": voice.register_guidance(contact_register),
    }


def run_relationship_agent(
    db,
    user_id: int,
    *,
    instruction: str = "",
    max_steps: int = 12,
    client: Any = None,
    on_proposal: Any = None,
) -> RelationshipAgentResult:
    """Run the propose-only relationship agent for one host.

    Loads the host's contacts, exposes read + propose tools, and runs the
    bounded loop. Returns staged proposals — NO sends, NO DB writes. The
    caller owns whether/when any proposal is acted on.

    `instruction` is an optional free-form steer from the host (the chat
    message, e.g. "who at Stripe should I ping?"). It's folded into the
    kickoff prompt as the host's ask; it does NOT grant new tools, so the
    propose-only guarantee is unchanged — the worst case is still a bad
    *suggestion*, never a bad send.
    """
    result = RelationshipAgentResult()

    contacts = relationships.list_contacts(db, user_id)
    result.contacts_seen = len(contacts)
    if not contacts:
        result.summary = "No contacts yet — nothing to work."
        result.stop_reason = "empty"
        return result

    # Index by id so the read/propose tools can resolve a contact_id without
    # re-querying. Owner-scoped already (list_contacts filters by user_id), so
    # a contact_id the model invents simply won't resolve.
    by_id = {c.id: c for c in contacts}

    def _stage(p: Proposal) -> None:
        # Single place a proposal lands: append to the result AND notify any
        # streaming caller so the chat can reveal each person the moment it's
        # drafted, instead of waiting for the whole loop to finish.
        result.proposals.append(p)
        if on_proposal is not None:
            try:
                on_proposal(p)
            except Exception:  # noqa: BLE001 : a slow/broken consumer must not break the run
                pass

    # ── tool implementations (closures over db + this run's contacts) ──────
    def _list_contacts() -> list[dict]:
        rows = []
        for c in contacts:
            s = relationships.contact_summary(db, c)
            contact_types = s.get("contact_types") or []
            # Cheap thread-derived recall signals so message-blind triage can
            # spot content-only open loops (host promise, unanswered question).
            # See _thread_signals for the recall/precision rationale.
            signals = _thread_signals(_thread_from_timeline(
                relationships.contact_timeline(db, c)))
            rows.append({
                "contact_id": c.id,
                "name": s.get("name") or "Unknown",
                "company": s.get("company") or "",
                "relationship_stage": s.get("relationship_stage"),
                "n_events": s.get("n_events"),
                "is_stale": bool(s.get("relationship_stage") == "stale"),
                "days_since_last_touch": _days_since(s.get("last_touch_at")),
                "has_next_step": bool((s.get("next_step") or "").strip()),
                "next_step": s.get("next_step") or "",
                # Capture-phase markers: what the host tagged this person as when
                # they met (sales / recruiting / follow_up / other). A
                # `follow_up` tag is an explicit "circle back to them" intent set
                # at capture time — a primary WHO signal, see the system prompt.
                "contact_types": contact_types,
                "marked_follow_up": "follow_up" in contact_types,
                **signals,
            })
        return rows

    def _get_contact(contact_id: int) -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        timeline = relationships.contact_timeline(db, c)
        # `prior_messages` is the actual host<->contact thread a draft must
        # continue — it's what grounds the message in real context, so it is sent
        # IN FULL: draft quality/voice must never lose history. Prompt caching
        # (agent_loop._mark_thread_cache) makes re-sending it on later steps
        # cheap, so there's no latency reason to trim it.
        #
        # The raw `timeline` is the event log, largely redundant with
        # prior_messages (extracted from it) and the `events` rollup, so we cap
        # IT to keep the first read light without touching draft grounding.
        return {
            "summary": relationships.contact_summary(db, c),
            "events": relationships.contact_events(db, c),
            "timeline": timeline[-12:],
            "prior_messages": _thread_from_timeline(timeline),
        }

    def _name_of(contact_id: int) -> str:
        c = by_id.get(int(contact_id))
        if c is None:
            return "Unknown"
        return relationships.contact_summary(db, c).get("name") or "Unknown"

    def _propose_next_step(contact_id: int, next_step: str, rationale: str = "") -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        _stage(Proposal(
            kind="next_step", contact_id=int(contact_id),
            contact_name=_name_of(contact_id),
            text=_strip_dashes(next_step), rationale=_strip_dashes(rationale)))
        return {"staged": True, "kind": "next_step", "contact_id": int(contact_id)}

    def _draft_message(contact_id: int, message: str, rationale: str = "") -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        _stage(Proposal(
            kind="draft_message", contact_id=int(contact_id),
            contact_name=_name_of(contact_id),
            text=_strip_dashes(message), rationale=_strip_dashes(rationale)))
        return {"staged": True, "kind": "draft_message", "contact_id": int(contact_id)}

    tool_impls = {
        "get_contact": _get_contact,
        "propose_next_step": _propose_next_step,
        "draft_message": _draft_message,
    }

    # Inline the roster instead of spending a whole sequential LLM turn on a
    # `list_contacts` round-trip: the survey is deterministic data, so handing it
    # to the model up front lets it go straight to get_contact -> draft. This is
    # the single biggest cut to time-to-first-card (one fewer Claude call before
    # anything streams).
    roster = _list_contacts()
    roster_json = json.dumps(roster, default=str)
    user_prompt = (
        f"You have {len(contacts)} contacts in the spine. Here is the full "
        f"roster (one row per person):\n{roster_json}\n\n"
        f"Read it, find who is going cold or lacks a next step, and propose "
        f"concrete moves. Deep-dive at most {MAX_DEEP_DIVES} people this run."
    )
    steer = (instruction or "").strip()
    if steer:
        # The host is driving via chat. Honor their ask first, but keep them on
        # the auditable rails: still survey the spine and read history before
        # proposing. If the ask names people/companies/criteria, prioritise
        # those; otherwise fall back to the cold/no-next-step heuristic above.
        user_prompt = (
            f"The host asked: \"{steer}\"\n\n"
            f"Answer their request by working the contact spine. {user_prompt} "
            f"Prioritise whoever the host's ask points at. Your closing line at "
            f"the end should directly answer what they asked, in one short "
            f"conversational sentence (no tables, no lists)."
        )

    # Speak in the host's voice: reuse the SAME captured voice_examples the
    # initial-message composer uses, appended as a <style_examples> primer so
    # follow-ups sound like the same person who sent the first DM.
    system = _SYSTEM_PROMPT + _voice_context_block(db, user_id)

    run = run_agent(
        system=system,
        tools=_TOOLS,
        tool_impls=tool_impls,
        user_prompt=user_prompt,
        max_steps=max_steps,
        client=client,
    )

    result.summary = run.final_text
    result.steps = run.steps
    result.stop_reason = run.stop_reason
    result.error = run.error
    return result


# ───────────────────────── concurrent variant ──────────────────────────────
#
# The sequential loop above pays for latency that scales with the number of
# people it works: each person is a fresh, SERIAL Claude turn (read -> draft),
# so time-to-all-cards is ~Σ(per-person draft). The drafts don't depend on each
# other, though, so we can split the run into two phases and parallelise the
# expensive one:
#
#   Phase 1 — TRIAGE  (one Sonnet call): the roster goes in, a ranked list of
#     who-to-follow-up-with + why comes out. Roster-signal only; no per-person
#     history yet. This is the single short "silent" window (covered by the
#     stream heartbeat).
#   Phase 1.5 — MATERIALISE CONTEXT (sequential, in the worker thread): pull
#     each selected person's history via the DB. Done BEFORE fan-out and on ONE
#     thread because a SQLAlchemy Session is not thread-safe — the parallel jobs
#     must never touch `db`.
#   Phase 2 — DRAFT  (parallel): one Sonnet call PER person, fanned out under a
#     bounded asyncio.Semaphore (same proven pattern as
#     outreach.prefetch_compose_all) so they run concurrently instead of
#     end-to-end. Each draft stages its card the instant it resolves, so the
#     host sees follow-ups stream in as they finish. Time-to-all-cards collapses
#     from ~Σ(draft) to ~max(draft).
#
# Safety is unchanged: the only "act" tools are propose_next_step / draft_message
# (staged, never sent). The draft phase ALSO gets `skip_contact`, the escape
# hatch that restores the loop's suppression rule (don't pile on if a follow-up
# already went out or they already replied) now that the full thread is in hand.

# Keep Sonnet for both phases — quality + voice matching depend on it. Same env
# override the loop honours, falling back to agent_loop's Sonnet default.
_AGENT_MODEL = (os.environ.get("RELATIONSHIP_AGENT_MODEL")
                or os.environ.get("AGENT_LOOP_MODEL")
                or DEFAULT_MODEL)
# Bounded fan-out width. The real ceiling is Anthropic's per-key rate limit, not
# local compute (so Modal buys nothing here): the semaphore is the throttle that
# keeps a 100-contact host from firing a dozen simultaneous Sonnet calls into a
# 429. 5 is a deliberately conservative middle of the 4-6 band (Sonnet tokens
# are heavier than the Haiku compose path that runs at 10).
_DRAFT_CONCURRENCY = int(os.environ.get("RELATIONSHIP_DRAFT_CONCURRENCY", "5"))
# Generous ceiling for the one triage call. The concurrent path nominates
# everyone with a plausible hook (no count cap), and each selection is ~50-60
# tokens of {contact_id, reason, angle}; on a large roster that's several kt of
# tool output. Too low a limit would truncate the select_followups JSON and
# silently drop tail nominees, so keep headroom.
_TRIAGE_MAX_TOKENS = 8192
_DRAFT_MAX_TOKENS = 1024


_AGENT_CLIENT = None


def _agent_client():
    """Shared Anthropic client for the concurrent path's parallel draft calls.

    A module singleton (not a per-call `Anthropic()`) for the exact reason
    outreach._compose_client documents: under a fan-out semaphore, per-call
    clients open one fresh TCP+TLS handshake each and Railway's egress melts
    them into APIConnectionError ("egress connection storm"). One pooled client
    + max_retries=2 reuses connections and absorbs a single 429/5xx blip.

    Key resolution reuses llm._api_key() so we strip the trailing newline the
    Railway dashboard appends to env vars (otherwise httpx rejects every request
    with an "Illegal header value" LocalProtocolError)."""
    global _AGENT_CLIENT
    if _AGENT_CLIENT is None:
        from anthropic import Anthropic
        from .llm import _api_key
        _AGENT_CLIENT = Anthropic(api_key=_api_key(), max_retries=2)
    return _AGENT_CLIENT


_TRIAGE_TOOL = {
    "name": "select_followups",
    "description": (
        "Pick who the host should follow up with NOW, ranked most-important "
        "first, from the roster signals. This is triage only: you are NOT "
        "drafting messages here, just choosing who deserves a draft and why."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selections": {
                "type": "array",
                "description": "The people to follow up with, highest priority first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "integer",
                                       "description": "contact_id from the roster."},
                        "reason": {"type": "string",
                                   "description": "One line: why follow up now, "
                                   "grounded in the roster signals."},
                        "angle": {"type": "string",
                                  "description": "The hook the follow-up should "
                                  "hit (a job change, time passing, an open loop)."},
                    },
                    "required": ["contact_id", "reason"],
                },
            },
            "closing": {"type": "string",
                        "description": "A SHORT, conversational closing line for "
                        "the host, one or two sentences, plain prose (no lists)."},
            "force_draft": {
                "type": "boolean",
                "description": (
                    "TRUE only when the host EXPLICITLY COMMANDED drafting "
                    "messages for the people you select — e.g. 'draft messages "
                    "for everyone', 'message these folks', 'write to X and Y', "
                    "'create drafts for all of them'. FALSE when the host merely "
                    "ASKED who to follow up with ('who should I reach out to?', "
                    "'anyone worth pinging?') or gave no instruction at all. When "
                    "TRUE, the drafting step honors the host's command and writes "
                    "a message for every person you select instead of holding off "
                    "on 'too soon' / 'ball in their court' grounds."),
            },
        },
        "required": ["selections"],
    },
}

_SKIP_TOOL = {
    "name": "skip_contact",
    "description": (
        "Decline to draft for this person because, after reading their actual "
        "conversation thread and provided context, a follow-up is not warranted "
        "right now.\n\n"
        "Skipping is normal and expected. This tool should be called whenever "
        "the thread does not show a genuine natural next action for the host.\n\n"
        "Use skip_contact when:\n"
        "- the loop is closed\n"
        "- they declined, said no, said not interested, or opted out\n"
        "- the matter is resolved\n"
        "- the ball is in the contact's court\n"
        "- the host messaged recently and it is too soon to nudge again\n"
        "- a real follow-up already went out and nothing has changed\n"
        "- the person merely attended, RSVP'd, was imported, or seems relevant, "
        "but there is no actual hook\n"
        "- they converted and there is no next action\n"
        "- a message would feel forced, repetitive, pushy, or disconnected from "
        "the thread\n\n"
        "Draft only when the actual thread or provided context shows a genuine "
        "open reason to reach out, such as a reply that needs an answer, an open "
        "loop, a concrete next_step, a promised follow-up, a stale warm "
        "relationship with a natural reconnect angle, or a clear update/trigger "
        "in the provided context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "integer"},
            "reason": {"type": "string",
                       "description": "Why no follow-up is warranted, grounded in "
                       "the thread content, one line."},
        },
        "required": ["contact_id"],
    },
}

# Phase-2 tools: act-only (propose / draft, both staged-not-sent) plus the skip
# escape hatch. No get_contact — the context is injected inline per person.
_DRAFT_TOOLS = [_TOOLS[1], _TOOLS[2], _SKIP_TOOL]  # propose_next_step, draft_message, skip


_TRIAGE_SYSTEM = """\
You are a relationship manager for an event host.

You are given the host's full contact list, one row per person, with the available relationship signals for each person. Your only job in this step is TRIAGE: nominate people who plausibly warrant a relationship action from the host, ranked most important first.

You do not write messages here. You do not make the final send/no-send decision.

A second step will read each nominee's actual conversation thread and decide, from the content, whether a follow-up is genuinely warranted. That step will skip anyone whose thread shows the loop is closed, they declined, no response is needed, or the ball is clearly in the contact's court.

Your job is high-recall triage, not final filtering.

Your goal is a wide net, not everyone.

Nominate people when the provided data shows a plausible reason for the host to take another relationship action now.

A plausible relationship action includes:
- following up on an open loop
- responding to someone who replied
- continuing momentum after a meaningful interaction
- reconnecting with someone who has gone quiet
- checking in after time has passed
- following up on a host-written next step
- reaching out because the provided data contains a clear life, work, company, or event update
- reviving a warm relationship where another touch would feel natural
- answering the host's explicit request

Wide net means include weak but real hooks.
Wide net does not mean include people with no hook.

CRITICAL EVIDENCE RULE:
Only use reasons that are explicitly present in the provided data.

Do not invent or assume:
- life updates
- company updates
- funding news
- job changes
- moves
- launches
- promotions
- personal milestones
- recent activity
- intent to reconnect
- interest level
- relationship warmth

If the provided data does not show the reason, do not use it.

The test is not:
"Could this person maybe be relevant?"

The test is:
"Does the provided data show a plausible reason for the host to take another relationship action now?"

ANSWER THE HOST'S REQUEST FIRST.

If the host typed a request that names a company, group, person, stage, event, tag, or criterion, nominate everyone who matches that request and is relevant to that request. This intent overrides the default heuristics. It can include people who already replied, converted, went stale, or would otherwise be deprioritized.

Examples:
- "Who at Stripe?" means nominate people the provided data identifies as connected to Stripe.
- "Who replied?" means nominate people whose provided data shows they replied.
- "Anyone who has gone cold?" means nominate people with stale or quiet-relationship signals.
- "Who did I mark for follow-up?" means nominate people marked for follow-up.
- "Any investors?" means nominate people the provided data identifies as investors.
- "Who should I reconnect with?" means nominate people with stale, warm, prior-engagement, or update-based reasons shown in the data.

Do not use outside knowledge. Do not search. Do not guess based on someone's name, company, title, or background unless the provided data supports it.

When there is no specific host request, or the request is open-ended, use the default triage rules below.

Some rows carry thread-derived signals computed from the actual message thread.
Treat these as authoritative when present:
- awaiting_host_reply: the contact spoke last and the host has not answered.
- awaiting_contact_reply: the host spoke last and is waiting on the contact.
- host_open_promise: the host committed to a next move (send something, make an intro, schedule, circle back) and has not done it yet.
- contact_open_question: the contact asked something the host has not answered.
- open_loop_detected / open_loop_type / open_loop_evidence: an unresolved thread, with the kind and the quoted evidence.
- followup_due: an open obligation that is no longer brand-new.
- last_message_from / last_message_age_days: who spoke last and how long ago.

MUST NOMINATE (any one of these is sufficient):
1. The person matches the host's explicit request.
2. marked_follow_up is true.
3. The host wrote a next_step (has_next_step is true).
4. relationship_stage is replied.
5. awaiting_host_reply is true.
6. host_open_promise is true.
7. contact_open_question is true.
8. open_loop_detected is true, or followup_due is true.
9. The data shows a clear life, work, company, or event update that makes outreach natural.
10. The relationship has gone quiet after meaningful prior engagement, based on is_stale, days_since_last_touch, or notes in the data.

USUALLY NOMINATE:
1. Warm contacts where the data shows a check-in would feel natural.
2. High-fit or high-value people where the data shows a concrete reason to continue the relationship.
3. Converted contacts when the data shows another plausible action, such as expansion, next event, referral, intro, feedback, renewal, or post-conversion check-in.
4. People who attended, applied, RSVP'd, or engaged only if the data shows a real continuation angle beyond attendance alone.

DO NOT NOMINATE:
1. People with no clear reason for another touch.
2. People whose only signal is that they exist in the contact list.
3. People who merely attended, RSVP'd, applied, or were imported, with no follow-up or reconnecting hook.
4. Recent un-replied first outreach where another message would just be piling on, UNLESS the data shows an open loop, a host promise, a due follow-up, a host-written next_step, a stale signal, a marked follow-up, or an explicit host-request match.
5. Clearly closed-loop contacts with no next action.
6. Declined, rejected, unsubscribed, not interested, or do-not-contact contacts, unless the host explicitly asked for them.
7. Converted contacts with no reason to continue.
8. People where the ball is clearly in the contact's court and there is no stale or reconnect trigger.
9. People who seem generally interesting, impressive, or relevant, but have no actionable relationship reason in the provided data.

Important distinctions:
- Replied does not mean done. A reply may need a response, so nominate replied contacts.
- Converted does not always mean done. Nominate converted contacts only when the data shows another plausible action.
- Unanswered does not automatically mean follow up. Only nominate unanswered contacts if they are stale, marked, have a next_step, match the host's request, or have another explicit hook.
- A recent unanswered message with no obligation is probably NOT a follow-up. But a recent unanswered message where the host promised something (host_open_promise) IS a must-nominate: the obligation sits with the host, not the contact.
- Attendance alone is not a follow-up reason. There must be a continuation hook.
- A title or company alone is not a follow-up reason unless the host explicitly asked for that title or company.
- Do not pre-filter by stage alone. Use the actual relationship-action hook.

Ranking priority:
1. Direct matches to the host's request.
2. Host-marked follow-ups and rows with next_step.
3. Replied or open-loop contacts.
4. Contacts with explicit life, work, company, or event updates in the data.
5. Stale live relationships.
6. High-fit or high-value contacts with a concrete next action.
7. Converted contacts with a clear reason to continue.

Within each tier, rank by:
1. clearest next action
2. strongest explicit hook
3. strongest relationship fit shown in the data
4. highest apparent value to the host
5. oldest unresolved touch
6. strongest match to the host's stated intent

Call select_followups exactly once with every person who is plausibly relevant, ranked most important first. Do not apply an arbitrary cap.

For each selected person, provide:
- the person identifier required by the tool
- a one-line reason based only on the provided data
- an angle for the later message-writing step

The reason and angle must not contain invented facts. If the hook is uncertain, state the uncertainty using the available signal. For example, say "possibly stale based on days_since_last_touch," not "they probably lost interest."

Also provide a short conversational closing line for the host. The closing line should be one or two plain-prose sentences. Do not use a table. Do not use bullets. If the host asked a specific question, answer it directly. If nobody is worth nominating, return an empty selections list and say so warmly."""


_DRAFT_SYSTEM = """\
You are a relationship manager for an event host.

You are deciding whether to draft ONE follow-up for ONE person. The person was nominated by a wide triage step, but that does not mean they should receive a message.

You are the real filter.

You are given the person's full relationship context inline below, including:
- a context_brief: a deterministic pre-read of the situation (relationship state, who owes the next move, the likely natural action, facts that are safe to use, facts to treat as uncertain, what not to repeat, and drafting risks)
- the full context as JSON: rollup summary, shared events, cross-event timeline, and prior_messages, which is the actual host/contact conversation thread in chronological order

The context_brief is a convenience, not an authority. It is built from loose signals. If anything in the context_brief conflicts with prior_messages, prior_messages wins. Never assert a fact that the brief lists under facts_to_avoid_or_treat_as_uncertain unless prior_messages independently confirms it.

Your first job is to read prior_messages and decide from the actual conversation content whether a follow-up is genuinely warranted right now.

Do not assume a follow-up is needed because the person was nominated.
Do not rely only on the triage reason.
Do not draft from vibes.
Do not invent new context, updates, warmth, intent, or obligations.

Use only the provided context.

The test is:

"Based on the actual thread and provided context, is there a natural next relationship action for the host to take now?"

If yes, draft.
If no, skip.

Skipping is normal and expected. Call skip_contact whenever the content does not show a genuine reason to message now.

SKIP when:

1. CLOSED LOOP
They declined, said no, said not interested, opted out, or the matter is resolved.

2. THEIR COURT
The contact has the next natural move. For example, the host already asked a question, proposed times, sent the requested resource, made the intro, or asked them to confirm, and the contact has not responded yet.

3. TOO SOON
The host messaged recently and there is no new reason to nudge again.

4. ALREADY HANDLED
A real follow-up already went out, and nothing in the provided context has changed since then.

5. NO REAL HOOK
The person is interesting, attended an event, RSVP'd, or exists in the contact list, but the thread does not show an open loop, reply to answer, next step, stale warm relationship, or concrete reconnect trigger.

6. CONVERTED AND DONE
They converted or completed the intended action, and the context does not show another natural next action.

7. UNSAFE OR AWKWARD
A follow-up would feel forced, repetitive, pushy, or disconnected from the actual conversation.

DRAFT only when the content shows a genuine open reason to reach out, such as:

1. The contact replied and their reply warrants a response.
When drafting after they replied, answer their message. Do not write a generic nudge.

2. There is an unanswered question or open loop for the host to address.

3. The host wrote a concrete next_step.

4. The host promised to send something, follow up, make an intro, share a link, schedule, check back, or continue a specific thread.

5. The relationship has gone quiet after meaningful prior engagement, and the provided context shows a natural reason to reconnect.

6. There is a clear update, event, milestone, or trigger in the provided context that makes outreach natural.

7. The contact showed interest, but the thread has not been moved forward.

Important distinctions:
- A nomination is not evidence that a message is needed.
- A reply is not automatically a reason to nudge. If they replied, decide whether the host needs to answer.
- An unanswered host message is not automatically a reason to follow up. If it is recent or the ball is clearly in their court, skip.
- A stale relationship is not automatically a reason to message. There must be a natural reconnect angle in the provided context.
- Attendance alone is not a reason to message.
- Conversion alone is not a reason to message again.
- Do not send "just checking in" unless the context supports a natural check-in.

Before you draft, weigh the context_brief's drafting_risks: if it flags TOO SOON or THEIR COURT and prior_messages does not show a new reason to reach out, prefer skip_contact.

If you draft:
- The message must be grounded in prior_messages and the provided context.
- Do not restate the host's do_not_repeat line from the brief; move the thread forward instead.
- The message must make sense as the next message in the existing thread.
- The message should be short, natural, and specific.
- Under 60 words unless the context clearly requires slightly more.
- No em dashes or en dashes.
- Match the host's style examples.
- Do not over-explain.
- Do not sound like a sales sequence.
- Do not mention internal labels such as triage, stale, open loop, converted, or relationship_stage.
- Do not mention that you read their history.
- Do not invent details, updates, or personal facts.

An undelivered promise is never a skip. If the host promised to send or do something (reason 4 above) and prior_messages does not show it was delivered, you MUST act. Prefer draft_message: write the short note that delivers or accompanies the promised thing (the host attaches the actual file or link when they send it), so you do NOT need the deliverable itself in the context to write that note. Only fall back to propose_next_step when no sendable message could carry the promise forward. Never skip an open promise — that lets the host's commitment fall through, which is a failure, not a safe hold.

If the best next action is not a message but a relationship action, call propose_next_step.
If a message is warranted, call draft_message.
If no message is warranted right now, call skip_contact.

When unsure, prefer skip_contact unless there is a concrete reason in the thread to message — but an open host promise IS such a concrete reason, so act on it rather than skip."""


def _tool_uses(resp: Any) -> list[Any]:
    """Pull the tool_use blocks out of a messages.create response."""
    return [b for b in (getattr(resp, "content", None) or [])
            if _block_type(b) == "tool_use"]


def _tu_input(block: Any) -> dict:
    """The input dict of a tool_use block (SDK object or plain dict)."""
    binp = getattr(block, "input", None)
    if binp is None and isinstance(block, dict):
        binp = block.get("input", {})
    return dict(binp or {})


def _tu_name(block: Any) -> str:
    return getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else "") or ""


def run_relationship_agent_concurrent(
    db,
    user_id: int,
    *,
    instruction: str = "",
    concurrency: int = _DRAFT_CONCURRENCY,
    client: Any = None,
    on_proposal: Any = None,
) -> RelationshipAgentResult:
    """Propose-only relationship agent, two-phase + parallel-draft variant.

    Same contract as run_relationship_agent (loads the host's spine, stages
    proposals, sends/writes NOTHING, fires `on_proposal` per staged proposal),
    but it triages in one call then drafts every selected person concurrently,
    so time-to-all-cards is ~max(draft) instead of ~Σ(draft). See the module
    note above for the phase breakdown and the thread-safety rationale (DB reads
    stay on this thread; only the Anthropic calls fan out).

    `client` is injected for tests; in production a pooled singleton is used.
    """
    result = RelationshipAgentResult()

    contacts = relationships.list_contacts(db, user_id)
    result.contacts_seen = len(contacts)
    if not contacts:
        result.summary = "No contacts yet — nothing to work."
        result.stop_reason = "empty"
        return result

    by_id = {c.id: c for c in contacts}
    cli = client or _agent_client()

    def _stage(p: Proposal) -> None:
        result.proposals.append(p)         # list.append is atomic under the GIL
        if on_proposal is not None:
            try:
                on_proposal(p)
            except Exception:  # noqa: BLE001 : a slow/broken consumer must not break the run
                pass

    # Reuse the roster builder + per-person reads from the loop variant's body
    # by recreating the same small closures (they only need `db`/`contacts`).
    def _roster() -> list[dict]:
        rows = []
        for c in contacts:
            s = relationships.contact_summary(db, c)
            contact_types = s.get("contact_types") or []
            # Thread-derived recall signals (who spoke last, open promises /
            # questions) so triage can see content-level open loops without
            # reading full threads. Cheap heuristics; Phase-2 is the precision
            # filter. See _thread_signals for the recall/precision rationale.
            signals = _thread_signals(_thread_from_timeline(
                relationships.contact_timeline(db, c)))
            rows.append({
                "contact_id": c.id,
                "name": s.get("name") or "Unknown",
                "company": s.get("company") or "",
                "relationship_stage": s.get("relationship_stage"),
                "n_events": s.get("n_events"),
                "is_stale": bool(s.get("relationship_stage") == "stale"),
                "days_since_last_touch": _days_since(s.get("last_touch_at")),
                "has_next_step": bool((s.get("next_step") or "").strip()),
                "next_step": s.get("next_step") or "",
                "contact_types": contact_types,
                "marked_follow_up": "follow_up" in contact_types,
                **signals,
            })
        return rows

    def _context(c) -> dict:
        timeline = relationships.contact_timeline(db, c)
        return {
            "summary": relationships.contact_summary(db, c),
            "events": relationships.contact_events(db, c),
            "timeline": timeline[-12:],
            "prior_messages": _thread_from_timeline(timeline),
        }

    def _name_of(contact_id: int) -> str:
        c = by_id.get(int(contact_id))
        return (relationships.contact_summary(db, c).get("name") or "Unknown") if c else "Unknown"

    draft_system = _DRAFT_SYSTEM + _voice_context_block(db, user_id)

    # ── Phase 1 : triage (one Sonnet call) ─────────────────────────────────
    roster_json = json.dumps(_roster(), default=str)
    triage_prompt = (
        f"You have {len(contacts)} contacts in the contact list. Here is the "
        f"full data, one row per person:\n\n{roster_json}\n\n"
        "Triage it: nominate every person with a plausible relationship action "
        "for the host to take now, ranked most important first.\n\n"
        "Use a wide net, but do not nominate everyone. Only nominate people "
        "where the provided data shows a real hook, such as:\n"
        "- marked for follow-up\n"
        "- host-written next_step\n"
        "- replied or open-loop\n"
        "- awaiting_host_reply, host_open_promise, contact_open_question, "
        "open_loop_detected, or followup_due in the thread signals\n"
        "- gone quiet after meaningful engagement\n"
        "- clear update or trigger in the provided data\n"
        "- concrete reason to reconnect or continue momentum\n\n"
        "Do not nominate people whose only signal is that they exist in the "
        "contact list, attended, RSVP'd, were imported, or seem generally "
        "interesting.\n\n"
        "Use only the provided data. Do not invent updates, interest, warmth, "
        "or reasons to reconnect."
    )
    steer = (instruction or "").strip()
    if steer:
        triage_prompt = (
            f"The host asked: \"{steer}\"\n\n"
            f"You have {len(contacts)} contacts in the contact list. Here is "
            f"the full data, one row per person:\n\n{roster_json}\n\n"
            "Triage it according to the host's request first.\n\n"
            "Nominate everyone who matches the host's request and has a relevant "
            "reason under that request. Rank them most important first. Do not "
            "apply an arbitrary cap.\n\n"
            "If the host's request is a COMMAND to draft/message/write to people "
            "(e.g. 'draft messages for everyone', 'message these folks', 'write "
            "to X and Y'), set force_draft TRUE and select everyone the command "
            "covers — the host has decided, so don't pre-filter on whether a "
            "touch is 'warranted'. If the host is only ASKING who to follow up "
            "with, set force_draft FALSE and use your judgment.\n\n"
            "If the host's request is specific, make the closing line directly "
            "answer what they asked in one short conversational sentence.\n\n"
            "Use only the provided data. Do not invent updates, interest, "
            "warmth, or reasons to reconnect."
        )

    try:
        triage_resp = cli.messages.create(
            model=_AGENT_MODEL,
            max_tokens=_TRIAGE_MAX_TOKENS,
            system=_TRIAGE_SYSTEM,
            tools=[_TRIAGE_TOOL],
            tool_choice={"type": "tool", "name": "select_followups"},
            messages=[{"role": "user", "content": triage_prompt}],
        )
    except Exception as exc:  # noqa: BLE001 : transport failure ends the run
        result.error = f"{type(exc).__name__}: {exc}"
        result.stop_reason = "error"
        return result

    selections: list[dict] = []
    closing = ""
    force_draft = False
    for tu in _tool_uses(triage_resp):
        if _tu_name(tu) == "select_followups":
            inp = _tu_input(tu)
            selections = list(inp.get("selections") or [])
            closing = (inp.get("closing") or "").strip()
            force_draft = bool(inp.get("force_draft"))
            break

    # Validate: keep only roster-resolvable ids (owner-scoping) and dedupe. No
    # arbitrary cap on count — triage nominates everyone with a plausible hook,
    # and the per-person content step is the real filter. The fan-out width is
    # still bounded by the _DRAFT_CONCURRENCY semaphore (concurrency, not total).
    seen: set[int] = set()
    clean: list[dict] = []
    for sel in selections:
        try:
            cid = int(sel.get("contact_id"))
        except (TypeError, ValueError):
            continue
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        clean.append({"contact_id": cid,
                      "reason": (sel.get("reason") or "").strip(),
                      "angle": (sel.get("angle") or "").strip()})

    result.steps = 1  # the triage call
    if not clean:
        result.summary = closing or "Everyone looks warm right now, nothing urgent to draft."
        result.stop_reason = "end_turn"
        return result

    # ── Phase 1.5 : materialise context SEQUENTIALLY (DB session is not
    # thread-safe, so all reads happen here, before any fan-out) ────────────
    jobs: list[dict] = []
    for sel in clean:
        c = by_id[sel["contact_id"]]
        jobs.append({"sel": sel, "ctx": _context(c), "name": _name_of(sel["contact_id"])})

    # Nominees a drafter declined via skip_contact (with the per-person reason),
    # so the closing summary reflects what was ACTUALLY staged, not what triage
    # promised before reading the threads. list.append is atomic under the GIL,
    # so the fan-out threads can record into this safely (same basis as _stage).
    skipped: list[dict] = []

    # ── Phase 2 : draft every selected person CONCURRENTLY (Anthropic-only,
    # no DB) under a bounded semaphore. Each draft stages its card the moment
    # it resolves, so they stream in as they finish. ───────────────────────
    def _draft_one(job: dict) -> None:
        sel, ctx, name = job["sel"], job["ctx"], job["name"]
        cid = sel["contact_id"]
        brief = _context_brief(sel, ctx)
        prompt = (
            f"Follow up with {name} (contact_id {cid}).\n\n"
            "<triage_signal>\n"
            "A message-blind, wide-net triage pass guessed: "
            f"\"{sel.get('reason') or 'they need a touch'}\" "
            f"(suggested angle: \"{sel.get('angle') or '(none given)'}\").\n"
            "These are LOW-CONFIDENCE GUESSES, not observations — triage never "
            "read the thread. This is NOT proof a message is needed, and any "
            "factual claim implied here (a promotion, raise, launch, new role, "
            "move, award, etc.) is UNCONFIRMED: do NOT state or congratulate it "
            "unless prior_messages explicitly shows it. If it isn't in the "
            "thread, write as if you don't know it.\n"
            "</triage_signal>\n\n"
            "<drafting_task>\n"
            + (
                # Host EXPLICITLY commanded a draft for this person: honor it.
                # The host has overridden the usual 'is it warranted?' judgment,
                # so do NOT second-guess it on timing/etiquette grounds.
                "The host EXPLICITLY asked you to draft a message for this "
                "person. Honor that: WRITE THE MESSAGE. Read prior_messages in "
                "<full_context_json> first so the draft continues the real "
                "thread (or opens naturally if there's no history yet), then "
                "draft it. Do NOT call skip_contact for 'too soon', 'ball in "
                "their court', 'no clear hook', or 'already handled' — the host "
                "has overridden that judgment. The ONLY time you may skip is a "
                "hard stop: the contact explicitly opted out, asked not to be "
                "contacted, or said no/not interested. Absent that, draft.\n"
                if force_draft else
                "Read prior_messages in <full_context_json> first. Decide "
                "whether a follow-up is genuinely warranted based on the actual "
                "thread.\n"
                "If there is a natural next relationship action for the host to "
                "take now, draft the message or propose the next step.\n"
                "If the thread shows the loop is closed, the ball is in their "
                "court, it is too soon, a follow-up was already handled, or "
                "there is no real hook, call skip_contact.\n"
            )
            + "</drafting_task>\n\n"
            "<context_brief>\n"
            "A deterministic pre-read of the signals. Use it to orient, but "
            "prior_messages is authoritative: if anything here conflicts with the "
            "actual thread, the thread wins.\n"
            + json.dumps(brief, default=str)
            + "\n</context_brief>\n\n"
            "<full_context_json>\n"
            + json.dumps(ctx, default=str)
            + "\n</full_context_json>"
        )
        resp = cli.messages.create(
            model=_AGENT_MODEL,
            max_tokens=_DRAFT_MAX_TOKENS,
            # Cache the (identical-across-people) system block so the 2nd..Nth
            # parallel draft read it from cache instead of reprocessing it.
            system=[{"type": "text", "text": draft_system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=_DRAFT_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": prompt}],
        )
        for tu in _tool_uses(resp):
            tname = _tu_name(tu)
            inp = _tu_input(tu)
            # Ignore a model slip that targets a different/invalid contact_id;
            # this job is about `cid` only.
            if tname == "skip_contact":
                skipped.append({
                    "contact_id": cid, "name": name,
                    "reason": _strip_dashes((inp.get("reason") or "").strip()),
                })
                continue
            if tname == "propose_next_step" and (inp.get("next_step") or "").strip():
                _stage(Proposal(
                    kind="next_step", contact_id=cid, contact_name=name,
                    text=_strip_dashes(inp["next_step"]),
                    rationale=_strip_dashes(inp.get("rationale") or "")))
            elif tname == "draft_message" and (inp.get("message") or "").strip():
                _stage(Proposal(
                    kind="draft_message", contact_id=cid, contact_name=name,
                    text=_strip_dashes(inp["message"]),
                    rationale=_strip_dashes(inp.get("rationale") or "")))

    async def _fan_out() -> None:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _bounded(job):
            async with sem:
                # _draft_one is sync (sync Anthropic client). Run it in a thread
                # so the gather actually parallelises the Claude round-trips.
                await asyncio.to_thread(_draft_one, job)

        # return_exceptions=True : a single draft that 429s past retries just
        # produces no card; the rest of the batch still completes cleanly.
        await asyncio.gather(*[_bounded(j) for j in jobs], return_exceptions=True)

    asyncio.run(_fan_out())

    # Reconcile the summary against what ACTUALLY got staged. The triage closing
    # was written BEFORE the per-person drafters ran, so it can promise a draft
    # ("bensiraphob gets a follow-up draft") that a drafter then declines via
    # skip_contact — leaving a summary that contradicts the cards on screen. If
    # any nominee was skipped, the closing is stale: rebuild a truthful wrap-up
    # from real outcomes. With no skips the closing is consistent, so keep its
    # warmer, question-answering phrasing.
    drafted_ids = {p.contact_id for p in result.proposals}
    held = [s for s in skipped if s["contact_id"] not in drafted_ids]
    if held:
        drafted_names = [j["name"] for j in jobs
                         if j["sel"]["contact_id"] in drafted_ids]
        result.summary = _reconcile_summary(drafted_names, held)
    else:
        result.summary = _strip_dashes(closing) if closing else (
            f"Drafted follow-ups for {len(result.proposals)} of your contacts.")
    result.stop_reason = "end_turn"
    return result
