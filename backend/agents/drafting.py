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
from typing import Optional

from . import relationships, voice
from .book import _btrace, _llm_json, stream_text  # shared Claude helpers + trace
from .relationship_agent import (
    _strip_dashes,
    _thread_from_timeline,
)

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
    "You write a short follow-up message for an event host reconnecting with "
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


def _email_thread_prior(db, user_id: int, contact) -> list[dict]:
    """The contact's REAL email conversation (bodies), shaped like the timeline
    thread ({when, who, channel, text}) so an email follow-up continues what was
    actually written. Uses the linked thread (Contact.email_thread_id) if set,
    else finds the newest thread with the contact's address. Best-effort: any
    missing piece (no mailbox, no address, Unipile error) returns []."""
    import os
    from .. import models
    from . import email_sync
    try:
        user = db.get(models.User, user_id)
        account_id = getattr(user, "unipile_email_account_id", None)
        own = (getattr(user, "email_account_address", None) or "").strip().lower()
        addr = (getattr(contact, "email", None) or "").strip().lower()
        dsn = (os.environ.get("UNIPILE_DSN") or "").strip()
        api_key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
        if not (account_id and dsn and api_key):
            return []
        thread_id = getattr(contact, "email_thread_id", None)
        if not thread_id and addr:
            threads = email_sync.list_threads_for_address(
                dsn=dsn, api_key=api_key, account_id=account_id,
                address=addr, own_address=own)
            thread_id = threads[0]["thread_id"] if threads else None
        if not thread_id:
            return []
        msgs = email_sync.thread_messages(
            dsn=dsn, api_key=api_key, account_id=account_id,
            thread_id=str(thread_id), own_address=own, with_bodies=True)
        prior = []
        for m in msgs:
            text = (m.get("body") or "").strip()
            if not text:
                continue
            prior.append({
                "when": m.get("date"),
                "who": "host" if m.get("direction") == "out" else "them",
                "channel": "email",
                "text": text[:600],
            })
        return prior
    except Exception:  # noqa: BLE001 : email grounding is best-effort
        return []


def _voice_block_for(db, user_id: int, channel: str) -> str:
    """The full model-ready voice context for this host: the distilled
    <host_voice_profile> rules PLUS the ground-truth <style_examples>, scoped to
    the channel being drafted. This is the same packaged voice the relationship
    agent uses -- richer than raw examples alone, which is what made earlier
    drafts read generic. DetachedInstance/lookup-safe (returns "")."""
    from .. import models
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


def _relationship_facts(db, contact) -> dict:
    """The grounding the composer needs to hone in on THIS relationship when the
    message thread is thin or absent (the common case): where/when the host met
    them, how long it's been, the relationship stage, and the host's own noted
    next step (the open loop). Pulled from the durable contact_summary rollup so
    a draft can say 'great meeting you at <event>' / 'as promised, <next step>'
    instead of a generic reconnect line. Best-effort: any read failure -> {}."""
    try:
        s = relationships.contact_summary(db, contact)
    except Exception:  # noqa: BLE001 : a summary read failure must not break drafting
        return {}

    def _clean(v):
        x = (str(v).strip() if v is not None else "")
        return "" if x.lower() in ("", "unknown", "none") else x

    # Their most recent detected activity (job change, new post, milestone) -- the
    # single strongest "hone in" signal: lets the draft reference what they're
    # ACTUALLY up to ("congrats on the new role", "saw your post on X") instead of
    # a generic line. Already in the DB via the updates engine -- no new scraping.
    upd = s.get("latest_update") or {}
    types = [t for t in (s.get("contact_types") or []) if t and str(t).strip()]

    # The headline ("New post" / "Started a role") AND the real content behind it
    # (the actual post text / the role detail), so the draft can reference real
    # substance -- "loved your post on inference infra" -- not just "saw your
    # update". The detail is the activity_update summary (already in the DB).
    head = _clean(upd.get("title"))
    detail = _clean(upd.get("summary"))

    # LOW-confidence color: what they do (their About / enriched works_on). Read
    # gracefully (contact.about may not exist yet -> None); filter the "general"
    # enrichment placeholder. Rendered as optional, never asserted.
    def _real_about(v):
        x = _clean(v)
        return "" if x.lower() in ("general", "general networking", "networking") else x
    ident = s.get("identity") or {}
    about = (_real_about(getattr(contact, "about", None))
             or _real_about(ident.get("works_on")) or _real_about(ident.get("bio")))
    return {
        "met_at": _clean(s.get("met_at")),               # event where they met
        "first_met_at": s.get("first_met_at"),           # datetime (oldest touch)
        "last_touch_at": s.get("last_touch_at"),
        "n_events": s.get("n_events") or 0,
        "stage": _clean(s.get("relationship_stage")),
        "next_step": _clean(s.get("next_step")),          # host's own open loop
        "latest_update": head or detail,                  # the headline
        "latest_update_detail": detail,                   # the real content
        "about": about[:240],                              # what they do (low-conf)
        "relationship_types": types,                       # sales / investor / hiring / ...
    }


def build_context(db, user_id: int, contact, voice_block: Optional[str] = None,
                  *, channel: str = "email") -> dict:
    """Gather everything the composer needs for `contact` via the DB (the host's
    voice + this person's real prior-message thread + the relationship grounding).
    Runs on the request thread; `voice_block` can be passed in pre-rendered to
    avoid re-loading it per person in a batch (it's the same for every contact of
    one host).

    For the EMAIL channel we also pull the real email-thread bodies so the draft
    continues the actual email conversation, not just a 'N messages' rollup."""
    name = (getattr(contact, "name", None) or "there").strip() or "there"
    try:
        prior = _thread_from_timeline(relationships.contact_timeline(db, contact))
    except Exception:  # noqa: BLE001 : a timeline read failure must not break drafting
        prior = []
    if channel == "email":
        email_prior = _email_thread_prior(db, user_id, contact)
        if email_prior:
            # Merge cross-channel history, oldest-first; email bodies are the
            # substance for an email follow-up. Dedup is unnecessary (the
            # timeline only carries an email ROLLUP, not these message bodies).
            prior = sorted(prior + email_prior,
                           key=lambda m: str(m.get("when") or ""))
    if voice_block is None:
        voice_block = _voice_block_for(db, user_id, channel)

    # Detect how THIS contact writes (formal/casual/neutral) from their own
    # messages, so the draft meets their register while keeping the host's voice.
    register = voice.detect_register(
        [m.get("text") or "" for m in prior if m.get("who") == "them"])

    def _real(v):
        s = (v or "").strip()
        return "" if s.lower() == "unknown" else s
    return {
        "name": name,
        # Person-specific facts so the draft can hone in on THIS contact even
        # when there's no prior thread (the common case -- DM history isn't in
        # the spine). Without these the model only has a name + reason and
        # generalizes.
        "company": _real(getattr(contact, "company", None)),
        "role": (_real(getattr(contact, "title", None))
                 or _real(getattr(contact, "headline", None))),
        "prior": prior,
        "register": register,
        # Relationship grounding (where/when met, open next step) so the draft is
        # specific to THIS relationship even with no message thread.
        "facts": _relationship_facts(db, contact),
        "voice_block": voice_block,
    }


def _natural_action(ctx: dict) -> str:
    """The single most natural move for THIS message, synthesized from the signals
    already in context, so the draft takes the right SHAPE (not just a warm
    blob): deliver on a promised next step, react to their news, reply to their
    last message, or re-engage after time. Deterministic, no LLM, no new data."""
    facts = ctx.get("facts") or {}
    prior = ctx.get("prior") or []
    them_last = bool(prior) and (prior[-1].get("who") == "them")
    if facts.get("next_step"):
        return (f"deliver on / pick up your own noted next step: "
                f"{facts['next_step']}")
    if facts.get("latest_update"):
        return (f"react warmly to their recent update ({facts['latest_update']}); "
                f"lead with that, congratulate, no hard ask")
    if them_last:
        return "they spoke last -- reply to their most recent message"
    if facts.get("stage") in ("stale", "dormant", "cooling"):
        return "re-engage warmly after time has passed, only with a natural angle"
    return ""


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
    optional: list[str] = []
    if facts.get("about"):
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


def _user_prompt(ctx: dict, reason: str, channel: str, directive: str = "") -> str:
    """RENDER: assemble the user message from the gathered+resolved context.
    `directive` is the host's free-form ask-bar instruction, shared across a
    batch; per-person facts keep each draft differentiated."""
    lines = [f"Who you're writing to: {_who(ctx)}."]
    asserted, optional = _select_grounding(ctx)
    if asserted:
        lines.append("What you know (verified facts you may reference): "
                     + "; ".join(asserted) + ".")
    if optional:
        lines.append("Optional color (use ONLY if it fits naturally, never force "
                     "it or overstate familiarity): " + "; ".join(optional) + ".")
    na = _natural_action(ctx)
    if na:
        lines.append(f"The natural move here: {na}.")
    lines += [
        "Prior conversation (oldest first; [] means no prior messages):",
        json.dumps(ctx.get("prior") or [], default=str),
        f"Reason to reach out now: {reason}",
        f"Channel: {channel}",
    ]
    directive = (directive or "").strip()
    if directive:
        lines.append(
            f"The host's instruction for this outreach (applies to everyone "
            f"they're writing to right now): {directive}. Honor it, but adapt it "
            f"to THIS person using the facts above -- do not paste the same line "
            f"to everyone.")
    return "\n".join(lines) + "\n"


def compose_from_context(ctx: dict, reason: str, channel: str = "email",
                         directive: str = "") -> Optional[dict]:
    """The pure-LLM half: compose from a context dict (no DB), so it's safe to
    fan out across threads. Returns {"subject", "body"} or None on failure.
    `directive` is the host's free-form ask-bar instruction (shared across the
    batch); per-person facts keep each draft differentiated."""
    system = _FOLLOWUP_SYSTEM + _resolve_voice(ctx)
    user = _user_prompt(ctx, reason, channel, directive)
    out = _llm_json(system, user, max_tokens=500)
    if not out or not (out.get("body") or "").strip():
        return None
    body = _strip_dashes(out["body"])
    subject = out.get("subject")
    subject = _strip_dashes(subject) if (channel == "email" and subject) else None
    return {"subject": subject, "body": body}


_FOLLOWUP_STREAM_SYSTEM = (
    "You write a short follow-up message for an event host reconnecting with "
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
                        directive: str = ""):
    """The pure-LLM half of streamed drafting: yield body tokens from a prebuilt
    context dict (no DB), so the agent can build all contexts serially then fan
    out token streams across threads. Mirrors compose_from_context, streamed.
    `directive` is the host's free-form ask-bar instruction (shared)."""
    system = _FOLLOWUP_STREAM_SYSTEM + _resolve_voice(ctx)
    user = _user_prompt(ctx, reason, channel, directive)
    yield from stream_text(system, user, max_tokens=500)


def compose_stream(db, user_id: int, contact, *, reason: str,
                   channel: str = "email"):
    """Yield the follow-up body token-by-token (live 'typing'). Same voice + real
    prior-thread context as compose_followup, but streamed as plain text (no JSON
    wrapper, so deltas render directly). For the streamed /draft tap. Yields
    nothing when no key is set -- the caller falls back to compose_followup."""
    yield from stream_from_context(build_context(db, user_id, contact),
                                   reason, channel)


def compose_followup(db, user_id: int, contact, *, reason: str,
                     channel: str = "email") -> Optional[dict]:
    """One voice-matched, thread-aware follow-up to `contact` (a Contact ORM row).
    Returns {"subject", "body"} or None on failure (caller falls back). Loads the
    thread + voice, then composes -- the single-draft contract used by /draft."""
    return compose_from_context(
        build_context(db, user_id, contact, channel=channel), reason, channel)


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
            jobs[i].get("directive") or directive)

    import time as _t
    t0 = _t.monotonic()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, concurrency)) as ex:
        list(ex.map(_one, range(len(jobs))))
    _btrace(f"compose_batch {len(jobs)} drafts (concurrency={concurrency}) "
            f"in {_t.monotonic()-t0:.2f}s")
    return results
