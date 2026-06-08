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
from .agent_loop import DEFAULT_MODEL, _block_type, run_agent

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
    at 8 to keep input tokens bounded (matches the composer's cap)."""
    from .. import models
    raw = ""
    try:
        user = db.get(models.User, user_id)
        if user is not None:
            raw = (getattr(user, "voice_examples", "") or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""
    if not raw:
        raw = (os.environ.get("OPERATOR_VOICE_EXAMPLES") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(s).strip() for s in parsed if str(s).strip()][:8]


def _voice_block(examples: list[str]) -> str:
    """Format the host's past messages as a style primer, mirroring the
    composer's <style_examples> block ('match their voice, not the content')
    so the two surfaces speak in one consistent voice."""
    if not examples:
        return ""
    lines = ["", "<style_examples>",
             "Past messages this host actually sent. Match their VOICE — "
             "greeting, sign-off, sentence length, formality, punctuation and "
             "emoji habits — not the content:"]
    for i, ex in enumerate(examples, 1):
        lines.append(f"---\nExample {i}:\n{ex}")
    lines += ["---", "</style_examples>"]
    return "\n".join(lines)


def _strip_dashes(text: str) -> str:
    """Belt-and-suspenders on the prompt's no-em-dash rule: rewrite any em/en
    dash to a comma, collapsing the surrounding spaces, so a model slip can't
    leak the AI 'tell' into a staged draft. Trailing/duplicate punctuation from
    the swap is tidied up."""
    import re
    out = re.sub(r"\s*[—–]\s*", ", ", text or "")
    out = re.sub(r",\s*([.!?,;:])", r"\1", out)   # ", ." -> "."
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


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
    system = _SYSTEM_PROMPT + _voice_block(_host_voice_examples(db, user_id))

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
# Must fit up to MAX_DEEP_DIVES ranked selections (each ~50-60 tokens of
# {contact_id, reason, angle}) plus the closing line in one triage call. At
# MAX_DEEP_DIVES=100 that's ~6k tokens of tool output; 8192 leaves headroom.
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
        },
        "required": ["selections"],
    },
}

_SKIP_TOOL = {
    "name": "skip_contact",
    "description": (
        "Decline to draft for this person. Use ONLY when the thread in "
        "prior_messages shows the follow-up is already handled: a SECOND host "
        "message went out after the first, OR the contact has already REPLIED "
        "(an inbound 'them' message). Absent those, draft instead of skipping."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "integer"},
            "reason": {"type": "string",
                       "description": "Why no draft is needed, one line."},
        },
        "required": ["contact_id"],
    },
}

# Phase-2 tools: act-only (propose / draft, both staged-not-sent) plus the skip
# escape hatch. No get_contact — the context is injected inline per person.
_DRAFT_TOOLS = [_TOOLS[1], _TOOLS[2], _SKIP_TOOL]  # propose_next_step, draft_message, skip


_TRIAGE_SYSTEM = (
    "You are a relationship manager for an event host. You are handed the "
    "host's FULL contact roster (one row per person, with the signals a survey "
    "returns). Your ONLY job this step is to TRIAGE: choose who the host should "
    "follow up with now, ranked most-important first. You do NOT write messages "
    "here.\n\n"
    "Choose from these signals (NOT from fresh external news), in PRIORITY ORDER:\n"
    "  a. MARKED follow-ups — AUTHORITATIVE: `marked_follow_up` true (the host "
    "tagged them 'follow_up') OR a `next_step` the host wrote down. The host has "
    "EXPLICITLY decided to follow up, so these are ALWAYS worth selecting this "
    "run, even if their last touch is recent or unanswered — an unanswered first "
    "outreach is the REASON to follow up, never a reason to wait. (The drafting "
    "step has the full thread and will skip anyone already handled, so when in "
    "doubt about a MARKED contact, select them.)\n"
    "  b. Stale, UNMARKED: a live relationship gone quiet past the stale line "
    "(`is_stale` true, or a large `days_since_last_touch`). BUT an unmarked "
    "contact whose only touch is a recent, un-replied first outreach should be "
    "held back — a second message this soon is piling on. Skip them this run.\n\n"
    "Call `select_followups` ONCE with up to "
    f"{MAX_DEEP_DIVES} people, ranked. Give each a one-line `reason` (why now) "
    "and an `angle` (the hook the follow-up should hit). Also give a short, "
    "conversational `closing` line for the host, like you're texting them back "
    "(one or two sentences, plain prose, NEVER a table or bullet list). If the "
    "roster has nobody worth actioning, return an empty `selections` list and "
    "say so warmly in `closing`."
)


_DRAFT_SYSTEM = (
    "You are a relationship manager for an event host, drafting ONE follow-up "
    "for ONE person whose full history is given to you inline below (rollup "
    "summary, the events you've shared, the cross-event timeline, and "
    "`prior_messages` — the actual host<->contact thread, oldest-first). A "
    "triage step already decided this person is worth following up with and "
    "told you why; your job is the draft.\n\n"
    "FIRST, check whether a follow-up is even needed. Suppress it — call "
    "`skip_contact` — ONLY if `prior_messages` shows a SECOND host message after "
    "the first (a real follow-up already went out) OR the contact has already "
    "REPLIED (an inbound 'them' message). Absent those, DRAFT.\n\n"
    "To draft, call `propose_next_step` (a specific action the host should take) "
    "and/or `draft_message` (a short, warm, specific message). The draft MUST "
    "build on `prior_messages`: pick up the thread where it left off, reference "
    "the initial context or what was already said, and only THEN add the new "
    "reason to reach out. The FIRST item in `prior_messages` is the initial "
    "message (the first DM or capture note); a follow-up reads as a continuation "
    "of THAT conversation, never a fresh cold open.\n\n"
    "Rules: Only use facts from the context provided — never invent an event, a "
    "name, or a detail. You CANNOT send anything; you only propose. Keep the "
    "draft under ~60 words, human, not salesy. NEVER use em dashes (—) or en "
    "dashes (–); use a comma, a period, or restructure. If a <style_examples> "
    "block is provided below, the draft MUST be written in the host's voice as "
    "shown there (greeting, sign-off, sentence length, formality, punctuation "
    "and emoji habits) — match the voice, not the content. This is the SAME "
    "voice the host's first message was written in."
)


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

    voice_block = _voice_block(_host_voice_examples(db, user_id))
    draft_system = _DRAFT_SYSTEM + voice_block

    # ── Phase 1 : triage (one Sonnet call) ─────────────────────────────────
    roster_json = json.dumps(_roster(), default=str)
    triage_prompt = (
        f"You have {len(contacts)} contacts in the spine. Here is the full "
        f"roster (one row per person):\n{roster_json}\n\n"
        f"Triage it: pick who is going cold or lacks a next step and rank them."
    )
    steer = (instruction or "").strip()
    if steer:
        triage_prompt = (
            f"The host asked: \"{steer}\"\n\n{triage_prompt} Prioritise whoever "
            f"the host's ask points at, and make your `closing` directly answer "
            f"what they asked in one short conversational sentence."
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
    for tu in _tool_uses(triage_resp):
        if _tu_name(tu) == "select_followups":
            inp = _tu_input(tu)
            selections = list(inp.get("selections") or [])
            closing = (inp.get("closing") or "").strip()
            break

    # Validate + cap: keep only roster-resolvable ids, dedupe, honour the cap.
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
        if len(clean) >= MAX_DEEP_DIVES:
            break

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

    # ── Phase 2 : draft every selected person CONCURRENTLY (Anthropic-only,
    # no DB) under a bounded semaphore. Each draft stages its card the moment
    # it resolves, so they stream in as they finish. ───────────────────────
    def _draft_one(job: dict) -> None:
        sel, ctx, name = job["sel"], job["ctx"], job["name"]
        cid = sel["contact_id"]
        prompt = (
            f"Follow up with {name} (contact_id {cid}). "
            f"Triage flagged them because: {sel.get('reason') or 'they need a touch'}."
            + (f" Angle to hit: {sel['angle']}." if sel.get("angle") else "")
            + "\n\nTheir full context:\n"
            + json.dumps(ctx, default=str)
            + "\n\nDraft the follow-up now (or skip_contact if already handled)."
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

    result.summary = _strip_dashes(closing) if closing else (
        f"Drafted follow-ups for {len(result.proposals)} of your contacts.")
    result.stop_reason = "end_turn"
    return result
