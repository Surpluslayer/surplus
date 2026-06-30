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

import concurrent.futures
import json
import os
from dataclasses import dataclass
from typing import Optional

from ...spine import relationships

from .... import voice
from ...book import _btrace, _llm_json, stream_text  # shared Claude helpers + trace
from ..agent.run import (
    _strip_dashes,
    _thread_from_timeline,
    _thread_signals,
)
from ..context.reconcile import apply_to_facts

# Foreground per-person draft fan-out width for compose_batch. Bounded so a
# multi-person /ask can't open a flood of Anthropic connections at once (same
# pool-saturation lesson as the book background gate). Tunable live.
_DRAFT_CONCURRENCY = max(1, int(os.environ.get("DRAFT_CONCURRENCY", "6")))


_SPECIFICITY = (
    "Hone in on THIS person. Name the concrete detail that makes the message "
    "obviously written for them, not a template: where you met them, your noted "
    "next step, their role/company, or the specific reason to reach out now. "
    "Never a generic line that would fit anyone (no \"hope you're doing well\", "
    "no \"just checking in\"). Use ONLY the facts given to you (the relationship "
    "grounding, the prior conversation, and the reason); never invent a meeting, "
    "a shared project, a mutual contact, or an update that is not stated. If you "
    "have no concrete detail beyond their name, reference the reason to reach out "
    "directly rather than padding with filler. "
)
_BREVITY = (
    "Keep it SHORT: 2-3 sentences, ideally under 45 words. Sound like a real "
    "person firing off a quick note, not a written-out email. No corporate "
    "warm-up, no restating their whole bio back to them. "
)
_VOICE_RULE = (
    "If a <host_voice_profile> and/or <style_examples> block is provided, write "
    "in that exact voice (greeting, sign-off, sentence length, punctuation, "
    "emoji habits), matching the voice not the content. If a Register line is "
    "given, meet the contact's formality while keeping the host's voice. "
    "NEVER use em dashes (—) or en dashes (–); use a comma, a period, or "
    "restructure. "
)


_FOLLOWUP_SYSTEM = (
    "You write a short follow-up message for the host reconnecting with "
    "someone they know. If prior messages are provided, CONTINUE that "
    "conversation: pick up where it left off and reference what was actually "
    "said, then add the reason to reach out now. If there are NO prior messages "
    "(the list is empty), write a warm, natural note built around the reason to "
    "reach out (e.g. congratulate them on the news) -- do NOT refuse, do NOT ask "
    "for more context, and do NOT mention the absence of prior messages; just "
    "write the message. "
    + _BREVITY + _SPECIFICITY + _VOICE_RULE +
    "If channel is email, also return a 3-5 word subject. "
    "Return ONLY JSON: {\"subject\":\"<email only, else null>\","
    "\"body\":\"<the message>\"}"
)


# ── two-phase split: DB read (serial, thread-unsafe) vs LLM call (concurrent) ──
#
# A multi-person /ask must draft many people, but a SQLAlchemy Session isn't
# thread-safe, so we can't touch the DB from the fan-out threads. Split the work:
#   build_context(db, ...)  -- all DB reads, on the request thread
#   compose_from_context()  -- pure LLM call, safe to run concurrently
# compose_followup() chains both for the single-draft (/draft tap) caller.


def _voice_block_for(db, user_id: int, channel: str) -> str:
    """The full model-ready voice context for this host: the distilled
    <host_voice_profile> rules PLUS the ground-truth <style_examples>, scoped to
    the channel being drafted. This is the same packaged voice the relationship
    agent uses -- richer than raw examples alone, which is what made earlier
    drafts read generic. DetachedInstance/lookup-safe (returns "")."""
    from ..... import models
    try:
        user = db.get(models.User, user_id)
    except Exception:  # noqa: BLE001 - keep the run alive on any lookup failure
        user = None
    vch = "email" if channel == "email" else "linkedin"
    return voice.build_voice_context(
        user, channel=vch, message_type="warm_followup")["block"]


def _months_ago(dt) -> str:
    """A coarse human relative-time label ('last week' / '~3 months ago') for a
    first-met datetime, or '' when missing/unparseable. Kept fuzzy on purpose:
    the draft says 'great catching up after a few months', never a false-precise
    date the host can't vouch for."""
    try:
        from datetime import datetime, timezone
        aware = relationships._as_aware(dt)
        if aware is None:
            return ""
        days = (datetime.now(timezone.utc) - aware).days
    except Exception:  # noqa: BLE001
        return ""
    if days < 0:
        return ""
    if days <= 10:
        return "recently"
    if days <= 45:
        return "a few weeks ago"
    months = max(1, round(days / 30))
    if months < 12:
        return f"~{months} months ago"
    years = round(days / 365)
    return "about a year ago" if years <= 1 else f"~{years} years ago"


def build_context(db, user_id: int, contact, voice_block: Optional[str] = None,
                  *, channel: str = "email") -> dict:
    """Gather everything the composer needs for `contact` via the DB (the host's
    voice + this person's real prior-message thread + the relationship grounding).
    Runs on the request thread; `voice_block` can be passed in pre-rendered to
    avoid re-loading it per person in a batch (it's the same for every contact of
    one host).

    For the EMAIL channel we also pull the real email-thread bodies so the draft
    continues the actual email conversation, not just a 'N messages' rollup."""
    from ..context.gather import as_composer_context, gather_contact_context
    gathered = gather_contact_context(
        db, user_id, contact,
        channel=channel,
        voice_block=voice_block,
        merge_email=True,
    )
    return as_composer_context(gathered)


def _natural_action(ctx: dict) -> str:
    """The single most natural move for THIS message, synthesized from the signals
    already in context, so the draft takes the right SHAPE (not just a warm
    blob): deliver on a promised next step, react to their news, reply to their
    last message, or re-engage after time. Deterministic, no LLM, no new data."""
    facts = ctx.get("facts") or {}
    prior = ctx.get("prior_full") or ctx.get("prior") or []
    sig = _thread_signals(prior)
    them_last = bool(prior) and (prior[-1].get("who") == "them")
    if facts.get("next_step"):
        return (f"deliver on / pick up your own noted next step: "
                f"{facts['next_step']}")
    if sig.get("contact_open_question"):
        ev = (sig.get("open_loop_evidence") or "").strip()
        return ("answer the question they asked"
                + (f": {ev}" if ev else ""))
    if sig.get("open_loop_type") == "schedule" or (
            sig.get("host_open_promise") and sig.get("open_loop_type") == "schedule"):
        ev = (sig.get("open_loop_evidence") or "").strip()
        return ("propose concrete times to meet or talk, matching how the thread "
                "framed it"
                + (f" ({ev})" if ev else ""))
    if sig.get("host_open_promise"):
        kind = sig.get("open_loop_type") or "what was promised"
        ev = (sig.get("open_loop_evidence") or "").strip()
        return (f"deliver on the host's open promise ({kind})"
                + (f": {ev}" if ev else ""))
    if facts.get("latest_update"):
        return (f"react warmly to their recent update ({facts['latest_update']}); "
                f"lead with that, congratulate, no hard ask")
    if them_last:
        last = (prior[-1].get("text") or "")
        low = last.lower()
        if any(h in low for h in (
                "coffee", "grab lunch", "find time", "schedule", "meet up",
                "jump on a call", "quick call", "sync", "call this week")):
            return ("they invited a meet-up in their last message — propose "
                    "concrete times or a place, matching how they framed it")
        return "they spoke last -- reply to their most recent message"
    if facts.get("stage") in ("stale", "dormant", "cooling"):
        return "re-engage warmly after time has passed, only with a natural angle"
    return ""


# ── Intent: what THIS message must accomplish ────────────────────────────────
# The explicit, hybrid form of "the move": a taxonomy `kind` (drives structure,
# eval cases, and per-kind guardrails) + a free-form `objective` (the specifics
# the agent or host supplies), plus optional hard constraints. Intent is purely
# ADDITIVE: when a caller passes none, the engine falls back to `_natural_action`
# and behaves exactly as before. Passing an intent is how the SAME engine writes
# any message (congratulate / intro / ask / ...), not just a follow-up. The agent
# will eventually DECIDE an intent and hand it here (see docs/draft-pipeline.md).
INTENT_KINDS = ("congratulate", "reengage", "intro", "ask", "schedule",
                "thank", "open", "reply", "followup")

_INTENT_GUIDE = {
    "congratulate": "congratulate them warmly; lead with the news, no ask",
    "reengage": "re-engage warmly after time has passed; low pressure, a natural angle",
    "intro": "offer a warm introduction; explain the why in one line and ask permission first",
    "ask": "make a specific, low-friction ask; be direct and easy to say yes to",
    "schedule": "propose a concrete time to meet or talk",
    "thank": "thank them specifically and sincerely; no ask",
    "open": "open a new conversation warmly; make clear why you're reaching out",
    "reply": "reply to their last message and move it forward",
    "followup": "follow up naturally on the relationship",
}


@dataclass(frozen=True)
class Intent:
    """What a single message is for. `kind` is one of INTENT_KINDS (falls back to
    'followup' if unknown); `objective` is the free-form per-message goal."""
    kind: str = "followup"
    objective: str = ""
    must: str = ""     # a hard inclusion ("mention the webinar Thursday")
    avoid: str = ""    # a hard exclusion ("don't pitch anything")


def _render_intent(intent: Intent) -> str:
    """Turn an Intent into the one goal line the RENDER stage states. The kind
    gives the SHAPE (from _INTENT_GUIDE), the objective gives the specifics."""
    parts = [_INTENT_GUIDE.get(intent.kind, intent.kind or "follow up naturally")]
    if (intent.objective or "").strip():
        parts.append(f"specifically: {intent.objective.strip()}")
    if (intent.must or "").strip():
        parts.append(f"must include: {intent.must.strip()}")
    if (intent.avoid or "").strip():
        parts.append(f"do not: {intent.avoid.strip()}")
    return "; ".join(parts)


def _first_nonempty(*parts: str) -> str:
    for p in parts:
        s = (p or "").strip()
        if s:
            return s
    return ""


# Loose phrase hooks for infer_intent — thread + angle beat directive when they
# conflict (call vs coffee is per-person; host batch intent is the fallback).
_SCHEDULE_HINTS = (
    "book a call", "schedule", "grab coffee", "grab lunch", "find time",
    "set up time", "catch up", "sync up", "calendar", "meet up", "meet for",
    "jump on a call", "quick call", "phone call", "video call", "zoom",
    "coffee chat", "get on a call", "hop on a call", "15 min", "20 min",
    "30 min",
)
_INTRO_HINTS = ("intro", "introduce", "connect you", "connect them", "introduction")
_ASK_HINTS = (
    "send the", "send over", "send through", "share the", "pricing", "deck",
    "proposal", "quote", "one-pager", "case study",
)
_CONGRAT_HINTS = (
    "congrat", "promoted", "promotion", "raised", "funding", "new role",
    "new job", "milestone", "launch",
)


def _mentions_any(text: str, phrases: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(p in low for p in phrases)


def _person_move(
    ctx: dict,
    *,
    natural_action: str = "",
    angle: str = "",
    sel_reason: str = "",
) -> str:
    """Per-person substance from the spine (thread, facts, Phase-2 angle) — what
    THIS contact specifically warrants, independent of the host's batch chat."""
    facts = ctx.get("facts") or {}
    prior = ctx.get("prior_full") or ctx.get("prior") or []
    sig = _thread_signals(prior)
    na = (natural_action or "").strip() or _natural_action(ctx)
    last_them = _first_nonempty(
        *(m.get("text") for m in reversed(prior) if m.get("who") == "them"))
    return _first_nonempty(
        facts.get("next_step"),
        sig.get("open_loop_evidence"),
        angle,
        na,
        facts.get("latest_update"),
        last_them,
        sel_reason,
    )


def _merge_host_and_person(person: str, host: str) -> str:
    """Combine host batch intent (chatbox / ask-bar) with per-person spine
    substance. Both apply: the host sets the campaign; the thread/facts set
    what this message is actually about for THIS contact."""
    person = (person or "").strip()
    host = (host or "").strip()
    if person and host:
        return (f"Host's campaign ask (MUST honor, adapted to this thread — never "
                f"paste verbatim): {host}. "
                f"Per-person hook for this contact: {person}.")
    return person or host


def compose_reason(
    ctx: dict,
    sel: Optional[dict] = None,
    angle: str = "",
    *,
    directive: str = "",
    natural_action: str = "",
) -> str:
    """Per-person reach-out line for the composer — merges spine substance AND
    the host's batch instruction when both are present."""
    sel = sel or {}
    person = _person_move(
        ctx,
        natural_action=natural_action,
        angle=angle,
        sel_reason=(sel.get("reason") or "").strip(),
    )
    return _merge_host_and_person(person, directive) or "following up"


def infer_intent(
    ctx: dict,
    *,
    angle: str = "",
    directive: str = "",
    sel_reason: str = "",
    natural_action: str = "",
) -> Intent:
    """Map host batch intent + per-person spine to an Intent. Thread/facts pick
    the shape (schedule/reply/…); objective merges BOTH host directive and
    person-specific move so a network-wide 'follow up' still honing per thread."""
    facts = ctx.get("facts") or {}
    prior = ctx.get("prior_full") or ctx.get("prior") or []
    sig = _thread_signals(prior)
    thread_text = " ".join(m.get("text") or "" for m in prior)
    combined = " ".join([
        directive, angle, sel_reason, natural_action,
        facts.get("next_step") or "", facts.get("latest_update") or "",
        thread_text, sig.get("open_loop_evidence") or "",
    ])

    kind = "followup"
    if sig.get("contact_open_question") or (
            prior and prior[-1].get("who") == "them"
            and "?" in (prior[-1].get("text") or "")):
        kind = "reply"
    elif sig.get("open_loop_type") == "schedule" or _mentions_any(
            " ".join([angle, thread_text, facts.get("next_step") or ""]),
            _SCHEDULE_HINTS):
        kind = "schedule"
    elif _mentions_any(combined, _INTRO_HINTS) or sig.get("open_loop_type") == "intro":
        kind = "intro"
    elif facts.get("latest_update") and (
            _mentions_any(combined, _CONGRAT_HINTS)
            or not _mentions_any(combined, _SCHEDULE_HINTS + _ASK_HINTS)):
        kind = "congratulate"
    elif _mentions_any(
            " ".join([angle, facts.get("next_step") or "", directive]),
            _ASK_HINTS) or sig.get("open_loop_type") in ("send_resource",):
        kind = "ask"
    elif _mentions_any(directive, _SCHEDULE_HINTS) and not thread_text.strip():
        kind = "schedule"
    elif facts.get("stage") in ("stale", "dormant", "cooling"):
        kind = "reengage"
    elif _mentions_any(directive, _SCHEDULE_HINTS):
        kind = "schedule"

    if kind not in INTENT_KINDS:
        kind = "followup"

    person = _person_move(
        ctx, natural_action=natural_action, angle=angle, sel_reason=sel_reason)
    objective = _merge_host_and_person(person, directive)
    if not objective and kind == "reply":
        objective = "answer their most recent message directly"
    return Intent(kind=kind, objective=objective)


def compose_inputs(
    ctx: dict,
    *,
    directive: str = "",
    angle: str = "",
    sel: Optional[dict] = None,
    natural_action: str = "",
) -> tuple[str, Intent]:
    """Single entry for the relationship agent: reason line + Intent, both fed
    from host batch intent AND per-person spine context."""
    sel = sel or {}
    reason = compose_reason(
        ctx, sel, angle, directive=directive, natural_action=natural_action)
    intent = infer_intent(
        ctx, angle=angle, directive=directive,
        sel_reason=(sel.get("reason") or "").strip(),
        natural_action=natural_action,
    )
    return reason, intent


# ─────────────────────────────────────────────────────────────────────────────
# The draft pipeline: GATHER (build_context, above) -> RESOLVE -> SELECT ->
# RENDER. Each stage is small + testable; the eval and every surface run the
# SAME pipeline. See ARCHITECTURE.md "the draft pipeline".
# ─────────────────────────────────────────────────────────────────────────────

# ── RESOLVE: voice strategy ──────────────────────────────────────────────────
# Three signals compete over "how should this sound": the host's voice profile,
# the contact's register, and the established thread dynamic. We resolve to ONE
# instruction by precedence -- thread dynamic > formal register > host profile --
# so the model never gets contradictory voice cues. (Mindset/grounding always
# outranks voice; that lives in the system prompt.)
_THREAD_MIRROR = (
    "\n(This is an ongoing conversation with this specific person. Continue the "
    "rapport and tone the two of you have ALREADY established in the prior "
    "messages, including any running topic, and subtly mirror how THEY write "
    "(message length, energy, formality, emoji) to build rapport, while keeping "
    "your own identity. This established dynamic takes priority over the voice "
    "profile.)")
_FORMAL_OVERRIDE = (
    "\n(This contact writes formally, so do NOT use casual tics: no slang, no "
    "emoji, no double exclamations. Write a warm but PROFESSIONAL note, a fuller "
    "greeting ('Hi <name>,' or 'Dear <name>,'), complete measured sentences. Keep "
    "the warmth, match the formality.)")


def _resolve_voice(ctx: dict) -> str:
    """The single voice instruction to append to the system prompt, resolved by
    precedence: FORMAL register > thread dynamic > host voice profile.

    Formal is a HARD constraint (no emoji/slang) and must outrank the thread
    mirror: a formal contact has to get a professional draft even mid-conversation
    (else the casual host voice leaks in -- the eval caught a casual 'Hey Dr.
    Vance! 🙌'). The thread mirror is for non-formal threads."""
    prior = ctx.get("prior") or []
    vb = ctx.get("voice_block") or ""
    if ctx.get("register") == "formal":
        return _FORMAL_OVERRIDE                     # drop casual, be professional
    if any(m.get("who") == "them" for m in prior):
        return vb + _THREAD_MIRROR                 # host identity + mirror the convo
    reg = voice.register_guidance(ctx.get("register"))   # casual/neutral nudge
    return vb + (f"\n(Register: {reg})" if reg else "")


# ── SELECT: grounding facts, ordered by relevance + gated by confidence ───────
# HIGH-confidence facts (verified: their update, your open loop, where you met)
# may be asserted in the draft. LOW-confidence color (what they do) is offered
# as optional, so anti-fabrication is structural, not a prompt plea. Facts are
# ordered strongest-first so the freshest signal leads.

def _select_grounding(ctx: dict) -> tuple[list[str], list[str]]:
    """Return (asserted, optional) grounding lines for THIS draft."""
    facts = ctx.get("facts") or {}
    asserted: list[str] = []
    if facts.get("latest_update"):
        detail = facts.get("latest_update_detail")
        extra = (f". What they actually said: \"{detail[:240]}\""
                 if detail and detail.strip() != facts["latest_update"].strip() else "")
        asserted.append(f"their most recent update: {facts['latest_update']}{extra}")
    if facts.get("next_step"):
        asserted.append(f"your own noted next step with them: {facts['next_step']}")
    if facts.get("met_at"):
        ago = _months_ago(facts.get("first_met_at"))
        asserted.append(f"you met them at {facts['met_at']}" + (f" ({ago})" if ago else ""))
    elif facts.get("n_events"):
        asserted.append(f"you've crossed paths at {facts['n_events']} event(s)")
    if facts.get("relationship_types"):
        asserted.append("how you know them: " + ", ".join(facts["relationship_types"][:3]))
    if facts.get("stage"):
        asserted.append(f"relationship stage: {facts['stage']}")
    # Knowledge-store facts (mode A), pre-phrased + confidence-split + de-duped
    # against the who-line in contact_memory.draft_grounding: high -> asserted,
    # low -> optional color.
    asserted += ctx.get("store_grounding") or []
    optional: list[str] = []
    optional += ctx.get("store_optional") or []
    if facts.get("about") and not (ctx.get("store_optional")):
        # legacy works_on/bio About only when the store has no real About fact
        optional.append(f"what they work on: {facts['about']}")
    return asserted, optional


# ── RENDER: assemble the user prompt from the resolved situation ──────────────

def _who(ctx: dict) -> str:
    name = ctx.get("name") or "there"
    role, company = ctx.get("role"), ctx.get("company")
    if role and company:
        return f"{name}, {role} at {company}"
    if role:
        return f"{name}, {role}"
    if company:
        return f"{name} at {company}"
    return name


def _user_prompt(ctx: dict, reason: str, channel: str, directive: str = "",
                 intent: Optional[Intent] = None) -> str:
    """RENDER: assemble the user message from the gathered+resolved context.
    `directive` is the host's free-form ask-bar instruction, shared across a
    batch; per-person facts keep each draft differentiated. `intent` is the
    explicit goal for THIS message; when None the move is derived from
    `_natural_action` exactly as before (behavior-neutral default)."""
    lines = [f"Who you're writing to: {_who(ctx)}."]
    directive = (directive or "").strip()
    if directive:
        lines.append(
            f"HOST CAMPAIGN ASK (required — weave this into the message for THIS "
            f"person, adapted to their thread; do not ignore or genericize it): "
            f"{directive}.")
    asserted, optional = _select_grounding(ctx)
    if asserted:
        lines.append("What you know (verified facts you may reference): "
                     + "; ".join(asserted) + ".")
    if optional:
        lines.append("Optional color (use ONLY if it fits naturally, never force "
                     "it or overstate familiarity): " + "; ".join(optional) + ".")
    if intent is not None:
        lines.append(f"Your goal for this message: {_render_intent(intent)}.")
    else:
        na = _natural_action(ctx)
        if na:
            lines.append(f"The natural move here: {na}.")
    summary = (ctx.get("thread_summary") or "").strip()
    if summary:
        lines.append(summary + ".")
    lines += [
        "Prior conversation (oldest first; [] means no prior messages):",
        json.dumps(ctx.get("prior") or [], default=str),
        f"Reason to reach out now: {reason}",
        f"Channel: {channel}",
    ]
    if directive:
        lines.append(
            "Honor the host campaign ask together with the per-person reason and "
            "thread — each draft must carry the campaign, not a copy-paste template.")
    return "\n".join(lines) + "\n"


def compose_from_context(ctx: dict, reason: str, channel: str = "email",
                         directive: str = "",
                         intent: Optional[Intent] = None) -> Optional[dict]:
    """The pure-LLM half: compose from a context dict (no DB), so it's safe to
    fan out across threads. Returns {"subject", "body"} or None on failure.
    `directive` is the host's free-form ask-bar instruction (shared across the
    batch); per-person facts keep each draft differentiated. `intent` is the
    optional explicit goal for THIS message (additive; None = today's behavior)."""
    system = _FOLLOWUP_SYSTEM + _resolve_voice(ctx)
    user = _user_prompt(ctx, reason, channel, directive, intent=intent)
    out = _llm_json(system, user, max_tokens=500)
    if not out or not (out.get("body") or "").strip():
        return None
    body = _strip_dashes(out["body"])
    subject = out.get("subject")
    subject = _strip_dashes(subject) if (channel == "email" and subject) else None
    return {"subject": subject, "body": body}


_FOLLOWUP_STREAM_SYSTEM = (
    "You write a short follow-up message for the host reconnecting with "
    "someone they met. If prior messages are provided, CONTINUE that "
    "conversation: pick up where it left off and reference what was actually "
    "said, then add the reason to reach out now. If there are NO prior messages "
    "(the list is empty), write a warm, natural note built around the reason to "
    "reach out; do NOT refuse, do NOT ask for more context, and do NOT mention "
    "the absence of prior messages. "
    + _BREVITY + _SPECIFICITY + _VOICE_RULE +
    "Write ONLY the message body as plain text: no subject line, no JSON, no "
    "surrounding quotes, no preamble or labels. Just the message to send."
)


def stream_from_context(ctx: dict, reason: str, channel: str = "email",
                        directive: str = "", intent: Optional[Intent] = None):
    """The pure-LLM half of streamed drafting: yield body tokens from a prebuilt
    context dict (no DB), so the agent can build all contexts serially then fan
    out token streams across threads. Mirrors compose_from_context, streamed.
    `directive` is the host's free-form ask-bar instruction (shared); `intent`
    is the optional explicit goal (additive; None = today's behavior)."""
    system = _FOLLOWUP_STREAM_SYSTEM + _resolve_voice(ctx)
    user = _user_prompt(ctx, reason, channel, directive, intent=intent)
    yield from stream_text(system, user, max_tokens=500)


def compose_stream(db, user_id: int, contact, *, reason: str,
                   channel: str = "email", intent: Optional[Intent] = None):
    """Yield the follow-up body token-by-token (live 'typing'). Same voice + real
    prior-thread context as compose_followup, but streamed as plain text (no JSON
    wrapper, so deltas render directly). For the streamed /draft tap. Yields
    nothing when no key is set -- the caller falls back to compose_followup."""
    yield from stream_from_context(build_context(db, user_id, contact),
                                   reason, channel, intent=intent)


def compose_followup(db, user_id: int, contact, *, reason: str,
                     channel: str = "email",
                     directive: str = "",
                     intent: Optional[Intent] = None) -> Optional[dict]:
    """One voice-matched, thread-aware follow-up to `contact` (a Contact ORM row).
    Returns {"subject", "body"} or None on failure (caller falls back). Loads the
    thread + voice, then composes -- the single-draft contract used by /draft.
    `directive` is the host's free-form ask (defaults to `reason` when omitted).
    `intent` is the optional explicit goal (additive; None = today's behavior)."""
    host_ask = (directive or reason or "").strip()
    return compose_from_context(
        build_context(db, user_id, contact, channel=channel), reason, channel,
        directive=host_ask, intent=intent)


def compose_batch(db, user_id: int, jobs: list[dict],
                  *, concurrency: int = _DRAFT_CONCURRENCY,
                  directive: str = "") -> list[Optional[dict]]:
    """Draft a follow-up for each job, returned in input order. Each job is
    {"contact": <Contact ORM>, "reason": str, "channel"?: str}. DB context is
    built SERIALLY here (session not thread-safe), then the LLM calls fan out
    under a bounded thread pool. Used by /ask to draft every selected person
    inline (voice + their real thread + dash scrub) without one-at-a-time waits.

    `directive` is the host's ask-bar instruction, shared across every job so the
    whole batch honors one intent (e.g. 'mention the webinar Thursday'); each
    draft still differs by its own reason + per-person facts. A job may override
    with its own "directive" key."""
    if not jobs:
        return []
    # Voice is per-host, identical across contacts: load once per channel, reuse.
    _vcache: dict[str, str] = {}

    def _vb(channel: str) -> str:
        if channel not in _vcache:
            _vcache[channel] = _voice_block_for(db, user_id, channel)
        return _vcache[channel]

    ctxs = [build_context(db, user_id, j["contact"],
                          _vb(j.get("channel") or "email"),
                          channel=(j.get("channel") or "email")) for j in jobs]
    results: list[Optional[dict]] = [None] * len(jobs)

    def _one(i: int) -> None:
        results[i] = compose_from_context(
            ctxs[i], jobs[i].get("reason") or "following up",
            jobs[i].get("channel") or "email",
            jobs[i].get("directive") or directive,
            intent=jobs[i].get("intent"))

    import time as _t
    t0 = _t.monotonic()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, concurrency)) as ex:
        list(ex.map(_one, range(len(jobs))))
    _btrace(f"compose_batch {len(jobs)} drafts (concurrency={concurrency}) "
            f"in {_t.monotonic()-t0:.2f}s")
    return results
