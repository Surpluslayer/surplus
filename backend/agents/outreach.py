"""
agents/outreach.py : stage 03b, message composition + simulated funnel.

  compose(prospect, event, peers=?, host_bio=?) -> Message
      Produces the LinkedIn connection note (≤280 chars) and the longer
      post-accept DM. Calls Claude (Haiku) to write a personalized message
      using prospect signal (role, company, works_on, offers, headline).
      Falls back to the deterministic template on any LLM failure so the
      pipeline can't be broken by a model outage.

  run_outreach(prospects, event, rng=?) -> [(prospect, events, status)]
      RNG-seeded simulator used in DRY_RUN mode for demo continuity.
"""
from __future__ import annotations
import asyncio
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config
from ..jsonx import extract_json
from ..providers.base import strip_em_dashes


# ---- compose result cache + prefetch -------------------------------------
#
# compose() is the slow path : ~3-5s per prospect (Haiku round-trip). The
# preview endpoint asks for every prospect's note + DM, so a 40-prospect
# event would block the UI for ~3 minutes if we composed sequentially on
# screen load. Two layers of speedup:
#
#   1. prefetch_compose_all() kicks off background compose tasks the moment
#      prospects are persisted (during prospecting). By the time the
#      operator reaches the auto-outreach screen, the results are usually
#      already in cache.
#   2. The cache is keyed by (prospect_id, event_id) with a 1h TTL so a
#      page refresh / nav-back / re-fetch reads instantly.
#
# Concurrency is capped via a semaphore to stay under Anthropic's per-key
# rate limits. The whole thing is best-effort : if a compose fails, the
# preview endpoint falls back to live compose() which has its own fallback
# chain (template).

_COMPOSE_CACHE: dict[tuple[int, int], tuple[float, "Message"]] = {}
_COMPOSE_CACHE_TTL_S = 60 * 60  # 1h
_COMPOSE_CONCURRENCY = 10


def get_cached_compose(prospect_id: int, event_id: int) -> "Message | None":
    """Return the cached composition for this (prospect, event) if fresh."""
    entry = _COMPOSE_CACHE.get((prospect_id, event_id))
    if entry is None:
        return None
    cached_at, msg = entry
    if time.time() - cached_at > _COMPOSE_CACHE_TTL_S:
        _COMPOSE_CACHE.pop((prospect_id, event_id), None)
        return None
    return msg


def _store_compose(prospect_id: int, event_id: int, msg: "Message") -> None:
    _COMPOSE_CACHE[(prospect_id, event_id)] = (time.time(), msg)


def reset_compose_cache() -> None:
    """Test hook : clears every cached entry. Production never calls this."""
    _COMPOSE_CACHE.clear()


async def prefetch_compose_all(prospects, event,
                              voice_examples_raw: str | None = None) -> None:
    """Fire compose() for every prospect, in parallel, results land in the
    per-(prospect, event) cache.

    Designed to be launched as a background task right after prospects are
    persisted: kicks off concurrent Claude calls while the operator is still
    looking at the prospecting progress screen. By the time they reach the
    auto-outreach screen, the cache is usually fully populated and the
    preview endpoint reads in <100ms.

    Best-effort: failures are swallowed (logged) so a single bad compose
    doesn't break the rest. The preview endpoint falls back to live compose
    for any cache miss it sees.

    `voice_examples_raw` : the caller (pipeline.py) pre-resolves
    `event.user.voice_examples` while its DB session is still open and
    passes the raw JSON string in. The background task can't do this
    lookup itself because the session is closed by then —
    `event.user` would raise DetachedInstanceError and crash compose().
    """
    if not prospects:
        return
    sem = asyncio.Semaphore(_COMPOSE_CONCURRENCY)

    async def _one(p):
        async with sem:
            try:
                # compose() is sync (uses the sync Anthropic client). Run it
                # in a thread so the gather can actually parallelize.
                msg = await asyncio.to_thread(
                    compose, p, event,
                    None, None, voice_examples_raw,
                )
                _store_compose(p.id, event.id, msg)
            except Exception as exc:  # noqa: BLE001
                print(f"  [prefetch_compose] {p.id} ({getattr(p, 'name', '?')}): "
                      f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*[_one(p) for p in prospects], return_exceptions=True)


# LinkedIn allows up to 300 chars in a connection note. We aim for 280 to
# leave room for unicode / smart-quote expansion and to keep things tight.
NOTE_CHAR_LIMIT = 280
NOTE_HARD_CAP = 300


@dataclass(frozen=True)
class Message:
    """Both halves of an outreach: the connection note + the post-accept DM."""
    note: str       # ≤NOTE_CHAR_LIMIT chars, fits in a LinkedIn connection request
    message: str    # longer follow-up, sent after the connection is accepted


def _truncate_note(text: str, limit: int = NOTE_CHAR_LIMIT) -> str:
    """Clip a note to `limit` chars cleanly on a sentence/word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    # try to end on a sentence boundary first
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        i = cut.rfind(sep)
        if i >= limit - 80:  # only accept a cut near the end
            return cut[: i + 1].strip()
    # otherwise end on a word boundary
    i = cut.rfind(" ")
    if i > 0:
        return cut[:i].rstrip(",. ") + "…"
    return cut.rstrip() + "…"


def _csv_first(v) -> str:
    """Pick the first non-empty entry from a CSV-stored multi-select column.
    Multi-select arrived after these templates existed; goal lookup needs a
    single key, and the seniority/co_stage placeholders read better with one
    value than a comma-joined string."""
    if not v:
        return ""
    return next((s.strip() for s in str(v).split(",") if s.strip()), "")


def _framing(event) -> str:
    """Render the per-goal outreach framing for one event. Picks the first
    goal when several are selected : keeps the demo coherent rather than
    awkwardly stuffing two goals into one sentence."""
    goal = _csv_first(event.goal) or "Hiring pipeline"
    seniority = _csv_first(event.seniority).lower() or "senior"
    co_stage = _csv_first(event.co_stage) or "Seed"
    return config.goal_cfg(goal)["outreach"].format(
        headcount=event.headcount,
        format=event.format.lower(),
        city=event.city,
        seniority=seniority,
        role=event.role.lower(),
        co_stage=co_stage,
    )


_COMPOSE_MODEL = os.environ.get("OUTREACH_COMPOSE_MODEL", "claude-haiku-4-5-20251001")
# Bumped to 30s default : Railway's Anthropic round-trip routinely needs
# >8s to even complete TCP/TLS handshake (manifesting as APIConnectionError,
# not APITimeoutError — anthropic-sdk wraps httpx.ConnectTimeout as the
# former). Local + Fly are both fine under 30s.
_COMPOSE_TIMEOUT_S = float(os.environ.get("OUTREACH_COMPOSE_TIMEOUT", "30"))


_COMPOSE_CLIENT = None


def _compose_client():
    """Shared anthropic client for compose calls.

    Earlier code instantiated `Anthropic()` per-call (= a new httpx
    Client + new TCP + new TLS handshake every time). With the prefetch
    semaphore at 10 concurrent calls, that meant 10 fresh handshakes
    every prospecting run. On Railway this consistently failed with
    APIConnectionError (egress connection storm) while the synchronous
    /outreach/preview path worked because it only fired one at a time.

    Using a module singleton + max_retries=2 fixes both : the SDK
    reuses connection pool entries and absorbs single 429/5xx blips.
    Same pattern as judge_relevance_batch's _client().
    """
    global _COMPOSE_CLIENT
    if _COMPOSE_CLIENT is None:
        from anthropic import Anthropic
        _COMPOSE_CLIENT = Anthropic(max_retries=2)
    return _COMPOSE_CLIENT
_COMPOSE_MAX_TOKENS = 800


_COMPOSE_SYSTEM = """You are writing personalized LinkedIn outreach for an event invitation.

You will produce two pieces:
  - note: the LinkedIn connection request note. MAX 280 characters (LinkedIn hard limit is 300; we leave headroom). Reference one specific, concrete thing about THIS recipient (pulled from their profile bio / headline / what they work on), not a generic compliment. End with a low-pressure question.
  - message: the first DM sent right after the connection is accepted. 3-6 sentences. Recap the event framing, weave in their specific background (role, company, what they work on), end with a soft ask to share details.

GROUND RULES
  - Reference REAL things about the recipient, drawn ONLY from the input: their role, company, About section, headline, and especially their recent LinkedIn posts. NEVER invent specifics (talks, projects, repos, articles) that aren't in the input.
  - The note MUST anchor on one real, specific detail about this person. When "THEIR RECENT LINKEDIN POSTS" is present, lead with a concrete callback to one of them : that recency is what makes outreach feel personal instead of templated.
  - If the only details you have are generic (e.g. just "Engineer" with no About / posts), keep the note short and honest rather than inventing a specific or padding with flattery.
  - When "WHAT THE HOST SAID ABOUT THE EVENT" is present, ground the event framing in the host's actual words : it's more specific and trustworthy than the generic per-goal framing line.
  - Match LinkedIn DM tone: warm, direct, no buzzwords, no "I came across your profile" filler.
  - Don't use em-dashes (LinkedIn auto-mangles them). Colons or commas instead.
  - Don't say "as an AI", don't apologize for reaching out.
  - Do NOT name other attendees / confirmed peers, even if you could. Keep it focused on the recipient and the event itself.
  - For the note: skip the greeting if you'd be over 280 chars; cut filler before content.

VOICE MATCHING
If the user message includes a `<style_examples>` block, those are real past
outreach messages the host has written. Mirror their:
  - sentence rhythm and length
  - vocabulary choices (avoid words they don't use)
  - opener style (e.g. "Hi <name>," vs "Hey <name>:" vs "Quick one for you,")
  - closer style (e.g. "Worth a chat?" vs "Open to it?" vs "Let me know.")
Do NOT copy specific facts from the examples (different recipient, different
event). Match the *voice*, not the content.

OUTPUT FORMAT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "note": "string, ≤280 chars",
  "message": "string"
}"""


def _get_voice_examples(event, voice_examples_raw: str | None = None) -> list[str]:
    """Resolve the voice-matching examples for this event's host.

    Order of preference:
      1. voice_examples_raw param (caller pre-resolved while DB session
         was still open : used by the background prefetch path which runs
         after the request session closes)
      2. event.user.voice_examples (JSON list of strings on the User row)
      3. OPERATOR_VOICE_EXAMPLES env var (JSON list of strings) : fallback
         for events created by the env-var operator before per-user
         examples existed
      4. [] : no style guide, compose falls back to generic personalization

    Bad JSON in any source is silently treated as empty so a typo can't
    break outreach. We cap at 8 examples to keep input tokens bounded.

    Defensive try/except : SQLAlchemy raises DetachedInstanceError when
    you access a relationship on an Event whose session has been closed
    (happens in prefetch_compose_all's background task, since the request
    session is gone by the time the task runs). Fall back to env var
    instead of crashing the whole compose() call.
    """
    import json
    raw = (voice_examples_raw or "").strip()
    if not raw:
        try:
            user = getattr(event, "user", None)
            if user is not None:
                raw = getattr(user, "voice_examples", "") or ""
        except Exception:  # noqa: BLE001 - DetachedInstanceError + friends
            raw = ""
    if not raw.strip():
        raw = (os.environ.get("OPERATOR_VOICE_EXAMPLES") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    examples = [str(s).strip() for s in parsed if str(s).strip()]
    return examples[:8]


def _compose_user_message(prospect, event, host_bio, framing,
                         voice_examples: list[str] | None = None,
                         relationship_ctx: str | None = None) -> str:
    """Pack everything the model needs to ground its output. Only facts we
    actually have go in : if a field is empty we omit it so Claude doesn't
    feel obligated to mention 'unknown'. Peer names are deliberately NOT
    passed in : the system prompt says not to drop names, and not having
    them in context removes the temptation entirely."""
    parts: list[str] = []

    # Voice examples go FIRST so they prime the model's tone before the
    # event/recipient context arrives.
    if voice_examples:
        parts.append("<style_examples>")
        parts.append("Past outreach messages from this host. Match their voice, not the content:")
        for i, ex in enumerate(voice_examples, 1):
            parts.append(f"---\nExample {i}:\n{ex.strip()}")
        parts += ["---", "</style_examples>", ""]

    parts += ["EVENT", f"Framing the host wants conveyed: {framing}"]
    brief = (getattr(event, "brief", "") or "").strip()
    if brief:
        # The host's own words about the event : richer + more specific than
        # the canned per-goal framing. Lead the model toward THIS, not the
        # template, when they differ.
        parts += ["", "WHAT THE HOST SAID ABOUT THE EVENT (their own words, "
                  "prefer this over the generic framing):", brief]
    if host_bio:
        parts += ["", "HOST BIO", host_bio.strip()]
    if event.format:
        parts.append(f"Format: {event.format}")
    if event.city:
        parts.append(f"City: {event.city}")

    # RECIPIENT context is REAL LinkedIn data only : headline, About, and
    # recent posts pulled live from their profile (see live_enrich.py), with
    # the Exa discovery snippet as fallback. We deliberately do NOT pass the
    # ICP-derived works_on / offers / seeks here : those are matcher guesses,
    # not facts about the person, and they're exactly what made past notes read
    # generic ("I see you work on X, come to my dinner").
    parts += ["", "RECIPIENT", f"Name: {prospect.name}",
              f"Role: {prospect.role}", f"Company: {prospect.company}"]
    if getattr(prospect, "headline", None):
        parts.append(f"Headline: {prospect.headline}")
    bio = (getattr(prospect, "bio", "") or "").strip()
    if bio:
        parts.append(f"About: {bio}")
    activity = (getattr(prospect, "recent_activity", "") or "").strip()
    if activity:
        # The strongest signal for a relevant note : something they actually
        # posted recently. Lead the note with a specific callback to this.
        parts += ["", "THEIR RECENT LINKEDIN POSTS (reference one specific, "
                  "current detail from here in the note — this is what makes it "
                  "land instead of reading generic):", activity]

    # Prior relationship history (compact, outbound-safe : never carries the
    # operator-only private_note). Background grounding, not to be quoted.
    ctx = (relationship_ctx or "").strip()
    if ctx:
        parts += ["", ctx]

    parts += ["", "Write the JSON now."]
    return "\n".join(parts)


def _compose_via_claude(prospect, event, host_bio, framing,
                       voice_examples_raw: str | None = None,
                       relationship_ctx: str | None = None) -> tuple[str, str] | None:
    """One Haiku call. Returns (note, message) or None on any failure
    (network, parse, missing fields). Caller falls back to the template."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return None
    try:
        client = _compose_client()
        t0 = time.time()
        resp = client.messages.create(
            model=_COMPOSE_MODEL,
            max_tokens=_COMPOSE_MAX_TOKENS,
            timeout=_COMPOSE_TIMEOUT_S,
            system=[{
                "type": "text", "text": _COMPOSE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[
                {"role": "user",
                 "content": _compose_user_message(prospect, event,
                                                  host_bio, framing,
                                                  voice_examples=_get_voice_examples(event, voice_examples_raw),
                                                  relationship_ctx=relationship_ctx)},
                # Prefill with "{" so Haiku stays in JSON mode.
                {"role": "assistant", "content": "{"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [compose] Claude failed: {type(exc).__name__}: {exc}")
        return None

    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full = "{" + "\n".join(text_chunks)
    parsed = extract_json(full)
    if not parsed:
        print(f"  [compose] couldn't parse JSON from Claude output ({len(full)} chars)")
        return None
    note = (parsed.get("note") or "").strip()
    message = (parsed.get("message") or "").strip()
    if not note or not message:
        return None
    print(f"  [compose] personalized for {prospect.name} in "
          f"{time.time() - t0:.1f}s (note: {len(note)}c, msg: {len(message)}c)")
    return note, message


def _compose_template(prospect, host_bio, framing) -> Message:
    """Deterministic fallback : the original template-based composition,
    minus the peer-reveal line (kept aligned with the LLM path, which no
    longer names peers either)."""
    first = (prospect.name or "there").split()[0]
    domain = (prospect.works_on or "your space").replace("-", " ")

    note_body = (
        f"Hi {first} : pulling together {framing}. "
        f"Your {domain} work caught my eye. "
        f"Worth your time?"
    )
    note = _truncate_note(note_body)

    msg_lines = [
        f"Thanks for connecting, {first}.",
        "",
        f"Quick context: we're putting together {framing}.",
    ]
    if host_bio:
        msg_lines += ["", host_bio.strip()]
    if prospect.offers:
        msg_lines += ["",
            f"Given your {domain} background ({prospect.offers}), "
            f"there's a clear fit on this side of the room."]
    msg_lines += ["", "Worth a closer look? Happy to share details."]
    return Message(note=note, message="\n".join(msg_lines).strip())


def compose(
    prospect,
    event,
    peers: list[str] | None = None,
    host_bio: str | None = None,
    voice_examples_raw: str | None = None,
    relationship_ctx: str | None = None,
) -> Message:
    """Build the connection note + post-accept DM for a prospect.

    Calls Claude (Haiku) to write personalized copy that references the
    recipient's actual role / company / works_on. Falls back to the
    deterministic template on any LLM failure so a model outage can't
    block the outreach pipeline.

    Set OUTREACH_COMPOSE_DISABLE=1 to skip the LLM entirely and always
    use the template (escape hatch for cost spikes / model issues).

    `voice_examples_raw` : optional pre-resolved JSON string of voice
    examples. The synchronous /outreach/preview path leaves this None
    and lets _get_voice_examples fetch event.user.voice_examples live
    (session is open). The background prefetch_compose_all path passes
    the value in because by the time the task runs, the request session
    is closed and event.user would raise DetachedInstanceError.
    """
    # `peers` is still accepted for callsite compatibility but intentionally
    # ignored : neither path names other attendees anymore.
    #
    # in_person events (the scan-to-connect entry point) get "we just met"
    # framing + template instead of the cold-invite copy. This branch lives in
    # compose() itself, NOT a separate function, so EVERY caller routes through
    # it : crucially the webhook auto-DM path (_trigger_auto_dm -> compose) so
    # an in-person prospect's post-accept DM is warm, not a cold re-pitch.
    in_person = (getattr(event, "kind", "") or "") == "in_person"
    framing = (_framing_inperson(event, getattr(prospect, "note", None),
                                 getattr(prospect, "next_step", None))
               if in_person else _framing(event))

    def _template() -> Message:
        return (_compose_inperson_template(prospect, event) if in_person
                else _compose_template(prospect, host_bio, framing))

    def _clean(msg: Message) -> Message:
        # Strip em/en dashes from EVERY compose path (template + LLM) so the
        # operator's /outreach/preview shows exactly what the LeadPayload
        # send-gate will transmit : preview == sent.
        return Message(note=strip_em_dashes(msg.note),
                       message=strip_em_dashes(msg.message))

    if (os.environ.get("OUTREACH_COMPOSE_DISABLE") or "").strip().lower() not in ("", "0", "false", "no"):
        return _clean(_template())

    llm = _compose_via_claude(prospect, event, host_bio, framing,
                              voice_examples_raw=voice_examples_raw,
                              relationship_ctx=relationship_ctx)
    if llm is not None:
        note, message = llm
        # Hard-cap the note even if the model went over : LinkedIn rejects >300.
        return _clean(Message(note=_truncate_note(note), message=message))
    return _clean(_template())


def _event_label(event) -> str:
    """Human-readable place for in-person copy : the in_person Event's label,
    falling back to event_name then a generic phrase."""
    return (getattr(event, "label", None)
            or getattr(event, "event_name", None) or "the event").strip()


def _framing_inperson(event, note: str | None = None,
                      next_step: str | None = None) -> str:
    """Warm 'we just met' framing for the in-person scan-to-connect flow.

    The operator has ALREADY met this person face to face, so the note/DM read
    as continuing a real conversation, not cold outreach. `note` is the
    operator's personal line about the conversation (prospect.note) : woven in
    so the LLM can reference something concrete they actually talked about.
    """
    label = _event_label(event)
    city = (getattr(event, "city", "") or "").strip()
    where = label + (f" in {city}" if city else "")
    parts = [
        f"You just met this person face to face at {where}.",
        "Write a warm LinkedIn connection note and first message that continue "
        "that conversation. The connection note must be BRIEF : one or two short "
        "sentences that lead with the specific thing you talked about, so it "
        "reads like a real callback rather than a template. Reference meeting in "
        "person, keep it friendly, and propose ONE concrete light next step (a "
        "quick call, a follow-up). Do NOT re-pitch the event or sound like a "
        "cold lead.",
    ]
    note = (note or "").strip()
    if note:
        parts.append(
            f"The specific thing you talked about: {note}. Open the connection "
            "note by referencing this directly (e.g. a fun fact like where "
            "they're from or a shared interest), so it feels personal.")
    next_step = (next_step or "").strip()
    if next_step:
        parts.append(
            f"For the first message, propose THIS specific next step: {next_step}. "
            "Work it in naturally as the closing ask (include any link verbatim).")
    return " ".join(parts)


def _compose_inperson_template(prospect, event) -> Message:
    """Deterministic in-person draft : used offline (no API key, dry-run) and
    as the fallback when the LLM call fails. Reads as a post-meeting note and
    weaves in prospect.note when present. Connection note is run through
    _truncate_note so it always fits LinkedIn's connect-request cap."""
    first = (prospect.name or "there").split()[0]
    label = _event_label(event)
    note = (getattr(prospect, "note", None) or "").strip()
    # Drop a conversational lead-in the operator may have typed ("we talked
    # about rock climbing" -> "rock climbing") so the template doesn't double up
    # on "about" / "chatting about".
    for _lead in ("we talked about ", "talked about ", "we chatted about ",
                  "chatted about ", "we discussed ", "discussed ", "about "):
        if note.lower().startswith(_lead):
            note = note[len(_lead):].strip()
            break
    # A preposition-led note ("from Ottawa", "into climbing") reads as a "you're
    # …" callback; everything else ("bagels", "her new startup") slots after
    # "about". Either way we LEAD with it so the note is a brief, real callback.
    is_fact = note.lower().split(" ")[0] in (
        "from", "in", "into", "based", "at") if note else False

    if note and is_fact:
        connection = _truncate_note(
            f"Great meeting you at {label}, {first} — love that you're {note}. "
            f"Let's connect!")
    elif note:
        connection = _truncate_note(
            f"Loved chatting about {note} at {label}, {first} — let's connect!")
    else:
        connection = _truncate_note(
            f"Great meeting you at {label}, {first} — let's connect!")

    chat = (f"Love that you're {note}" if (note and is_fact)
            else "Enjoyed our chat" + (f" about {note}" if note else ""))
    # The closing line is the operator's chosen next step when they set one
    # (e.g. "grab a coffee — book a time: <calendly>"); otherwise the default
    # light ask.
    step = (getattr(prospect, "next_step", None) or "").strip()
    closer = (f"Would love to {step}" if step else
              "Worth grabbing 15 minutes next week to pick it back up? Happy to "
              "work around your schedule, or just say the word if there's "
              "something I can help with sooner.")
    message = "\n".join([
        f"Great to meet you at {label}, {first}.",
        "",
        chat + " : wanted to connect here so we can keep it going.",
        "",
        closer,
    ]).strip()
    return Message(note=connection, message=message)


def compose_followup(prospect, event) -> str:
    """The follow-up DM sent N hours after the first post-accept message
    when the prospect hasn't replied. Lighter touch than the first DM :
    no re-pitch, explicit off-ramp."""
    first = (prospect.name or "there").split()[0]
    framing = _framing(event)
    lines = [
        f"Hey {first} : circling back on the {event.format.lower()}.",
        "",
        f"Quick recap: {framing}. Seats are filling so wanted to make sure "
        f"this didn't get lost.",
        "",
        "If it's not the right fit or timing, totally fine : just let me know "
        "and I'll close the loop. Otherwise happy to share details.",
    ]
    return "\n".join(lines).strip()


def run_outreach(prospects, event, rng: random.Random | None = None):
    """
    Simulated outreach funnel (used in DRY_RUN mode for demo continuity).

    Returns: list of (prospect, outreach_events, status) where outreach_events
    is a list of {"state", "body", "ts"} dicts in send order.
    """
    rng = rng or random.Random(event.id or 0)
    # callers (pipeline.run_outreach_stage) gate by status; we accept whoever
    # they hand us. status='approved' is the post-split contract.
    confirmed = [p for p in prospects if p.status == "approved"]
    all_names = [p.name for p in confirmed]
    results = []

    for p in confirmed:
        peers = [n for n in all_names if n != p.name]
        msg = compose(p, event, peers=peers)
        now = datetime.now(timezone.utc)
        events = [{"state": "sent", "body": msg.note, "ts": now}]

        # higher fit -> higher open + reply rates
        if rng.random() < min(0.97, 0.55 + p.fit_score / 200):
            events.append({"state": "opened", "body": "", "ts": now + timedelta(hours=2)})
            if rng.random() < min(0.90, 0.30 + p.fit_score / 160):
                events.append(
                    {"state": "replied", "body": "RSVP confirmed", "ts": now + timedelta(hours=6)}
                )

        status = "rsvp" if any(e["state"] == "replied" for e in events) else "contacted"
        results.append((p, events, status))

    return results
