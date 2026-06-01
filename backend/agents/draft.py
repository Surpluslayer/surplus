"""
draft.py : intent-steered "connect" message drafting.

The engine behind the in-person "Preview note + follow-up" step. Given who the
contact is, who the sender is, why they want to connect (the INTENT), and a short
note on what they talked about, it returns two things:

    { "connection_note": "<=200 char request note", "first_message": "<follow-up>" }

Intent is the strongest lever : Sales / Hiring / Networking / Vibes each produce a
genuinely different draft, and a freeform "other" string is treated as the literal
goal. See SYSTEM below.

Implementation notes
- Model defaults to claude-sonnet-4-6 (override via DRAFT_MODEL). We deliberately
  do NOT prefill the assistant turn with "{" the way agents/outreach.py does for
  Haiku : last-assistant-turn prefills return a 400 on Sonnet 4.6. We rely on the
  JSON-only instruction + extract_json (fence/prose tolerant) instead.
- Reuses the shared pooled Anthropic client from agents/outreach (single TCP/TLS
  pool : avoids the Railway egress connection storm a per-call client caused).
- Always falls back to a deterministic, intent-aware template when the API key is
  missing (tests / offline) or the call fails, so the endpoint never hard-errors.
"""
from __future__ import annotations
import os
import time

from ..jsonx import extract_json
from .outreach import _compose_client

DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "claude-sonnet-4-6")
DRAFT_MAX_TOKENS = int(os.environ.get("DRAFT_MAX_TOKENS", "512"))
DRAFT_TIMEOUT_S = float(os.environ.get("DRAFT_TIMEOUT", "30"))

# LinkedIn caps connection notes at 200 chars (free) / 300 (premium). Target 200
# to stay safe for everyone; raise NOTE_MAX if all your users are premium.
NOTE_MAX = int(os.environ.get("DRAFT_NOTE_MAX", "200"))


SYSTEM = """\
You write LinkedIn outreach for someone who just met a new contact at an in-person event.
You are given who the contact is, why the user wants to connect, and a short note on what
they talked about. You produce two things: a connection-request note, and a first message
to send once the request is accepted.

Voice:
- Plain English. Short sentences. No buzzwords, no "synergies," no "circle back," no "excited to."
- Do not use em dashes. Use periods or commas.
- Sound like a real person who just had a good conversation, not a salesperson or a template.
- Lead with the specific thing they talked about. Never invent details that were not provided.
- Use the contact's first name once. Do not over-flatter.

Length:
- connection_note: one short line, max 200 characters. No hard ask.
- first_message: 2 to 4 short sentences. Reference the conversation, give the reason for
  reaching out, then a low-pressure next step the contact can ignore without friction.

Match the tone to the intent:
- "Sales": they could be a customer. Be curious about their problem, not your product.
  No pitch. The next step is an offer to share how others handle the thing they mentioned.
- "Hiring": you may want to recruit them. Be specific about what impressed you. Hint at
  opportunity without making an offer. Keep it flattering and concrete.
- "Networking": peers, mutual value, no transaction. Find the shared thread, keep it light,
  suggest staying in touch or trading notes.
- "Vibes": you just liked them, no agenda. Warm and human, zero ask. "Good to meet you."
- Anything else (freeform text): treat the text as the literal goal. Let it set the tone
  and the next step. If it says "advice on fundraising," ask for that, specifically.

If the "what they talked about" note is empty, keep both messages generic but still warm,
and do not pretend to remember a detail you were not given.

If a booking link is provided, use it as the next step in the first_message (a low-pressure
"grab time here if useful" with the link), unless the intent is "Vibes" which carries no ask.
Never put a link in the connection_note.

Return ONLY valid JSON. No markdown, no backticks, no preamble:
{"connection_note": "string", "first_message": "string"}"""


USER_TMPL = """\
Contact
- Name: {contact_name}
- Headline: {contact_headline}

From (me)
- {sender_name}, {sender_role} at {sender_company}

Why I want to connect: {intent}
What we talked about: {context}{booking}"""


def _truncate_note(note: str, limit: int = NOTE_MAX) -> str:
    """Trim a connection note to <= limit chars on a sentence/word boundary."""
    note = (note or "").strip()
    if len(note) <= limit:
        return note
    cut = note[:limit]
    # Prefer the last sentence end, then the last space, then a hard cut.
    for sep in (". ", "! ", "? "):
        i = cut.rfind(sep)
        if i >= limit * 0.5:
            return cut[: i + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= limit * 0.5 else cut).strip()


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "there"


def _template(contact_name, intent, context, booking_link) -> dict[str, str]:
    """Deterministic, intent-aware fallback. Works offline and in tests : warm,
    references the talked-about note when present, never invents a detail."""
    first = _first_name(contact_name)
    talked = (context or "").strip()
    key = (intent or "").strip().lower()
    cta = f" If it is useful, grab a time here: {booking_link}" if booking_link else ""

    if talked:
        note = f"Great talking about {talked}, {first}. Let's stay connected."
        opener = f"Good meeting you at the event, {first}. I enjoyed our chat about {talked}."
    else:
        note = f"Good to meet you, {first}. Let's stay connected."
        opener = f"Good meeting you at the event, {first}. Glad we got to talk."

    if key == "sales":
        body = ("Happy to share how a few other teams have handled that, no pitch."
                f"{cta or ' Let me know if that would help.'}")
    elif key == "hiring":
        body = ("You clearly know your stuff. I would love to keep in touch about what "
                f"we are building.{cta or ' No pressure either way.'}")
    elif key == "networking":
        body = ("Would be good to trade notes sometime."
                f"{cta or ' No agenda, just keeping good people close.'}")
    elif key == "vibes":
        body = "No agenda here, just glad we crossed paths. Hope the rest of the event is good."
    else:  # freeform / other : reflect the goal literally
        body = (f"Reaching out about {intent.strip()}." if (intent or '').strip()
                else "Wanted to keep the conversation going.") + cta

    return {"connection_note": _truncate_note(note),
            "first_message": f"{opener} {body}".strip()}


def draft_connect(*, contact_name: str, contact_headline: str = "",
                  sender_name: str, sender_role: str = "", sender_company: str = "",
                  intent: str, context: str = "",
                  booking_link: str | None = None) -> dict[str, str]:
    """Return {connection_note, first_message} for a just-met contact.

    intent is passed through verbatim : a preset label (Sales/Hiring/Networking/
    Vibes) OR the user's freeform "other" text, which is the highest-signal input.
    Falls back to a deterministic template on missing key / failure / bad parse.
    """
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return _template(contact_name, intent, context, booking_link)

    booking = (f"\nMy booking link: {booking_link}" if (booking_link or "").strip() else "")
    user = USER_TMPL.format(
        contact_name=contact_name or "(unknown)",
        contact_headline=contact_headline or "(none)",
        sender_name=sender_name or "(me)",
        sender_role=sender_role or "",
        sender_company=sender_company or "",
        intent=intent or "Networking",
        context=context.strip() if (context or "").strip() else "(nothing noted)",
        booking=booking,
    )
    try:
        client = _compose_client()
        t0 = time.time()
        resp = client.messages.create(
            model=DRAFT_MODEL,
            max_tokens=DRAFT_MAX_TOKENS,
            timeout=DRAFT_TIMEOUT_S,
            system=[{
                "type": "text", "text": SYSTEM,
                # Caches once the prompt clears the model's min prefix; cheap
                # insurance and consistent with the rest of the codebase.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [draft] Claude failed: {type(exc).__name__}: {exc}")
        return _template(contact_name, intent, context, booking_link)

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = extract_json(text)
    note = (parsed or {}).get("connection_note", "")
    message = (parsed or {}).get("first_message", "")
    if not (note and message):
        print(f"  [draft] unparseable / empty output ({len(text)} chars); using template")
        return _template(contact_name, intent, context, booking_link)
    print(f"  [draft] {intent!r} draft for {contact_name} in {time.time() - t0:.1f}s")
    return {"connection_note": _truncate_note(note.strip()),
            "first_message": message.strip()}
