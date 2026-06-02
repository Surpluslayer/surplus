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
    "2. Prioritise: people who are STALE (no touch in a while) or who have a "
    "live relationship but no planned next step matter most. Ignore people who "
    "were just touched or already have a clear next step.\n"
    "3. For each person worth acting on, call `get_contact` to read their full "
    "history (events shared, stage, timeline) BEFORE deciding anything. Never "
    "propose a move without reading the history first.\n"
    "4. Propose ONE concrete move per person you act on: either "
    "`propose_next_step` (a specific action the host should take) and/or "
    "`draft_message` (a short, warm, specific message grounded in real shared "
    "history — reference the actual event/where you met, never generic). "
    "Quality over quantity.\n"
    "5. When you've worked the priority list, stop and give a one-paragraph "
    "summary of what you found and proposed.\n\n"
    "Rules: Only use facts returned by the tools — never invent an event, a "
    "name, or a detail. You CANNOT send anything; you only propose. Keep "
    "drafts under ~60 words and human, not salesy."
)


_TOOLS = [
    {
        "name": "list_contacts",
        "description": (
            "Survey the host's entire durable contact spine. Returns one row "
            "per person with name, company, strongest relationship stage, "
            "number of shared events, whether they're stale, days since last "
            "touch, and any existing next step. Call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_contact",
        "description": (
            "Read one person's full history before deciding: rollup summary, "
            "the events you've shared, and the cross-event timeline of every "
            "touch. Always call this before proposing a move for someone."
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
            "Draft a short, warm outreach message grounded in real shared "
            "history. Staged for the host to review and send — this does NOT "
            "send anything."
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


def _days_since(dt: Any) -> Optional[int]:
    aware = relationships._as_aware(dt)
    if aware is None:
        return None
    return (datetime.now(timezone.utc) - aware).days


def run_relationship_agent(
    db,
    user_id: int,
    *,
    max_steps: int = 12,
    client: Any = None,
) -> RelationshipAgentResult:
    """Run the propose-only relationship agent for one host.

    Loads the host's contacts, exposes read + propose tools, and runs the
    bounded loop. Returns staged proposals — NO sends, NO DB writes. The
    caller owns whether/when any proposal is acted on.
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
            })
        return rows

    def _get_contact(contact_id: int) -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        return {
            "summary": relationships.contact_summary(db, c),
            "events": relationships.contact_events(db, c),
            "timeline": relationships.contact_timeline(db, c),
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
            text=next_step.strip(), rationale=(rationale or "").strip()))
        return {"staged": True, "kind": "next_step", "contact_id": int(contact_id)}

    def _draft_message(contact_id: int, message: str, rationale: str = "") -> dict:
        c = by_id.get(int(contact_id))
        if c is None:
            return {"error": f"no contact {contact_id} for this host"}
        result.proposals.append(Proposal(
            kind="draft_message", contact_id=int(contact_id),
            contact_name=_name_of(contact_id),
            text=message.strip(), rationale=(rationale or "").strip()))
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

    run = run_agent(
        system=_SYSTEM_PROMPT,
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
