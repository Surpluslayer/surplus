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

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import relationships
from .agent_loop import run_agent

# How many contacts the agent may pull full history for in one run. A soft
# guard on cost/latency : the survey tool returns everyone, but deep-diving
# all of them would be wasteful. The model is told to prioritise.
MAX_DEEP_DIVES = 12


_SYSTEM_PROMPT = (
    "You are a relationship manager for an event host. You work their durable "
    "contact spine — the people they've met across events — and keep those "
    "relationships from going cold.\n\n"
    "Your loop each run:\n"
    "1. Call `list_contacts` to survey everyone.\n"
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
    "5. When you've worked the priority list, stop and give a one-paragraph "
    "summary of what you found and proposed.\n\n"
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
        "name": "list_contacts",
        "description": (
            "Survey the host's entire durable contact spine. Returns one row "
            "per person with name, company, strongest relationship stage, "
            "number of shared events, whether they're stale, days since last "
            "touch, any existing next step, the capture-time `contact_types` "
            "the host tagged them with, and `marked_follow_up` (true if the "
            "host explicitly tagged them 'follow_up' at capture). Call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
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
                               "description": "The contact_id from list_contacts."},
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
                              "description": "What history this is grounded in."},
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
        return {
            "summary": relationships.contact_summary(db, c),
            "events": relationships.contact_events(db, c),
            "timeline": timeline,
            # The actual message/note thread, pulled OUT of the timeline so the
            # model can't miss it: this is the conversation a follow-up must
            # CONTINUE (the first DM, the capture note, any reply). Oldest-first.
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
        result.proposals.append(Proposal(
            kind="next_step", contact_id=int(contact_id),
            contact_name=_name_of(contact_id),
            text=_strip_dashes(next_step), rationale=_strip_dashes(rationale)))
        return {"staged": True, "kind": "next_step", "contact_id": int(contact_id)}

    def _draft_message(contact_id: int, message: str, rationale: str = "") -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        result.proposals.append(Proposal(
            kind="draft_message", contact_id=int(contact_id),
            contact_name=_name_of(contact_id),
            text=_strip_dashes(message), rationale=_strip_dashes(rationale)))
        return {"staged": True, "kind": "draft_message", "contact_id": int(contact_id)}

    tool_impls = {
        "list_contacts": _list_contacts,
        "get_contact": _get_contact,
        "propose_next_step": _propose_next_step,
        "draft_message": _draft_message,
    }

    user_prompt = (
        f"You have {len(contacts)} contacts in the spine. Survey them, find who "
        f"is going cold or lacks a next step, and propose concrete moves. Deep-"
        f"dive at most {MAX_DEEP_DIVES} people this run."
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
            f"Prioritise whoever the host's ask points at. Your one-paragraph "
            f"summary at the end should directly answer what they asked."
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
