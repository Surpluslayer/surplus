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
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config
from ..jsonx import extract_json


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
_COMPOSE_TIMEOUT_S = float(os.environ.get("OUTREACH_COMPOSE_TIMEOUT", "8"))
_COMPOSE_MAX_TOKENS = 800


_COMPOSE_SYSTEM = """You are writing personalized LinkedIn outreach for an event invitation.

You will produce two pieces:
  - note: the LinkedIn connection request note. MAX 280 characters (LinkedIn hard limit is 300; we leave headroom). Reference one specific thing about the recipient. End with a low-pressure question.
  - message: the first DM sent right after the connection is accepted. 3-6 sentences. Recap the event framing, weave in their specific background (role, company, what they work on), end with a soft ask to share details.

GROUND RULES
  - Reference REAL things from the recipient's profile : their role, company, what they work on, what they offer. NEVER invent specifics (talks, projects, repos, articles) that aren't in the input.
  - Match LinkedIn DM tone: warm, direct, no buzzwords, no "I came across your profile" filler.
  - Don't use em-dashes (LinkedIn auto-mangles them). Colons or commas instead.
  - Don't say "as an AI", don't apologize for reaching out.
  - Do NOT name other attendees / confirmed peers, even if you could. Keep it focused on the recipient and the event itself.
  - For the note: skip the greeting if you'd be over 280 chars; cut filler before content.

OUTPUT FORMAT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "note": "string, ≤280 chars",
  "message": "string"
}"""


def _compose_user_message(prospect, event, host_bio, framing) -> str:
    """Pack everything the model needs to ground its output. Only facts we
    actually have go in : if a field is empty we omit it so Claude doesn't
    feel obligated to mention 'unknown'. Peer names are deliberately NOT
    passed in : the system prompt says not to drop names, and not having
    them in context removes the temptation entirely."""
    parts = ["EVENT", f"Framing the host wants conveyed: {framing}"]
    if host_bio:
        parts += ["", "HOST BIO", host_bio.strip()]
    if event.format:
        parts.append(f"Format: {event.format}")
    if event.city:
        parts.append(f"City: {event.city}")

    parts += ["", "RECIPIENT", f"Name: {prospect.name}",
              f"Role: {prospect.role}", f"Company: {prospect.company}"]
    if getattr(prospect, "works_on", None):
        parts.append(f"What they work on: {prospect.works_on}")
    if getattr(prospect, "offers", None):
        parts.append(f"Offers / strengths: {prospect.offers}")
    if getattr(prospect, "headline", None):
        parts.append(f"Headline: {prospect.headline}")

    parts += ["", "Write the JSON now."]
    return "\n".join(parts)


def _compose_via_claude(prospect, event, host_bio,
                       framing) -> tuple[str, str] | None:
    """One Haiku call. Returns (note, message) or None on any failure
    (network, parse, missing fields). Caller falls back to the template."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic()
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
                                                  host_bio, framing)},
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
) -> Message:
    """Build the connection note + post-accept DM for a prospect.

    Calls Claude (Haiku) to write personalized copy that references the
    recipient's actual role / company / works_on. Falls back to the
    deterministic template on any LLM failure so a model outage can't
    block the outreach pipeline.

    Set OUTREACH_COMPOSE_DISABLE=1 to skip the LLM entirely and always
    use the template (escape hatch for cost spikes / model issues).
    """
    # `peers` is still accepted for callsite compatibility but intentionally
    # ignored : neither path names other attendees anymore.
    framing = _framing(event)

    if (os.environ.get("OUTREACH_COMPOSE_DISABLE") or "").strip().lower() not in ("", "0", "false", "no"):
        return _compose_template(prospect, host_bio, framing)

    llm = _compose_via_claude(prospect, event, host_bio, framing)
    if llm is not None:
        note, message = llm
        # Hard-cap the note even if the model went over : LinkedIn rejects >300.
        return Message(note=_truncate_note(note), message=message)
    return _compose_template(prospect, host_bio, framing)


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
