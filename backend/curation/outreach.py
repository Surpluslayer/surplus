"""
curation/outreach.py : Stage 4 personalized outreach for ingested attendees.

Generates a per-recipient outreach message using:
  - the Event's framing (existing _framing() helper from agents/outreach.py)
  - the attendee's enrichment (firmographic + role + seniority)
  - the attendee's intended slot (high_fit_invite | gap_fill | reminder)

Returns the message text and writes an LLMCall audit row. Falls back to a
deterministic template when ANTHROPIC_API_KEY is unset so the pipeline
still works offline.

This module DELIBERATELY only emits text. Sending lives in providers/
and routes/. Curation attendees don't carry LinkedIn provider state the
way Prospects do, so we leave the wire-up to the operator.
"""
from __future__ import annotations
import json
import os
from typing import Literal

from sqlalchemy.orm import Session

from .. import models
from ..agents.outreach import _framing  # reuse the existing per-goal framer
from . import claude_log, enrichment as enrich_mod


_OUTREACH_MODEL = os.environ.get("CURATION_OUTREACH_MODEL", "claude-haiku-4-5-20251001")
_OUTREACH_TIMEOUT_S = float(os.environ.get("CURATION_OUTREACH_TIMEOUT", "10"))

Slot = Literal["high_fit_invite", "gap_fill", "reminder"]

_SLOT_INSTRUCTIONS = {
    "high_fit_invite": (
        "This is a high-fit invitation. Lead with WHY they specifically "
        "match the room. Aim for confidence : the operator is choosing them."
    ),
    "gap_fill": (
        "This is a gap-fill outreach : we need their profile-type to round "
        "out the room. Lead with what the room currently lacks that they "
        "uniquely add. Honest, no flattery."
    ),
    "reminder": (
        "This is a soft reminder : they've been invited but haven't RSVP'd. "
        "Recap the value, give an explicit off-ramp."
    ),
}


_OUTREACH_SYSTEM = """You write personalized event-invitation messages.

GROUND RULES
  - Reference ONE real, specific thing from the recipient's data. Never invent.
  - Match a warm, direct tone. No buzzwords, no "I came across your profile."
  - 4-7 sentences. Plain text. Don't use em-dashes (use colons or commas).
  - End with a low-pressure question or off-ramp, matched to the slot.
  - Don't claim AI authorship; don't apologize for reaching out.

OUTPUT
Return ONLY a JSON object: {"subject": "string", "body": "string"}."""


def compose_for_attendee(
    db: Session,
    attendee: models.Attendee,
    event: models.Event,
    *,
    slot: Slot = "high_fit_invite",
) -> dict:
    """Compose one outreach message for `attendee` at `event`.

    Returns {"subject": str, "body": str, "method": "llm"|"template"}.
    Always succeeds : LLM failures fall through to the template.
    """
    enrichment = enrich_mod.get_enrichment(attendee)
    framing = _framing(event)
    first_name = ((attendee.name or "").strip().split() or ["there"])[0]

    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        claude_log.log_disabled(
            db, purpose="outreach",
            event_id=event.id, attendee_id=attendee.id,
        )
        return _template_message(attendee, event, slot, first_name, framing)

    user_prompt = json.dumps({
        "slot": slot,
        "slot_instruction": _SLOT_INSTRUCTIONS[slot],
        "event": {
            "format": event.format, "city": event.city,
            "framing": framing,
        },
        "recipient": {
            "name": attendee.name, "first_name": first_name,
            "role": attendee.role, "company": attendee.company,
            "enrichment": enrichment,
            "list_source": attendee.list_source,
        },
    }, indent=2)

    with claude_log.log_call(
        db, purpose="outreach", model=_OUTREACH_MODEL,
        event_id=event.id, attendee_id=attendee.id,
        prompt=f"SYSTEM:\n{_OUTREACH_SYSTEM}\n\nUSER:\n{user_prompt}",
    ) as call:
        try:
            from anthropic import Anthropic
            client = Anthropic()
            resp = client.messages.create(
                model=_OUTREACH_MODEL,
                max_tokens=600,
                timeout=_OUTREACH_TIMEOUT_S,
                system=[{"type": "text", "text": _OUTREACH_SYSTEM,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": "{"},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            call.status = "error"
            call.error = f"{type(exc).__name__}: {exc}"
            return _template_message(attendee, event, slot, first_name, framing)

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        full = "{" + text
        call.output = full

        from ..jsonx import extract_json
        parsed = extract_json(full)
        if not parsed or not parsed.get("body"):
            call.status = "parse_error"
            return _template_message(attendee, event, slot, first_name, framing)

        return {
            "subject": (parsed.get("subject") or "").strip()
                       or _default_subject(event, slot),
            "body": parsed["body"].strip(),
            "method": "llm",
            "slot": slot,
        }


def _default_subject(event: models.Event, slot: Slot) -> str:
    fmt = event.format or "event"
    if slot == "reminder":
        return f"Quick follow-up on the {fmt}"
    if slot == "gap_fill":
        return f"One open seat for the {fmt}"
    return f"Invite: {fmt} in {event.city or 'town'}"


def _template_message(
    attendee: models.Attendee, event: models.Event,
    slot: Slot, first_name: str, framing: str,
) -> dict:
    role = attendee.role or "your work"
    company = f" at {attendee.company}" if attendee.company else ""
    if slot == "high_fit_invite":
        body = (
            f"Hi {first_name},\n\n"
            f"We're pulling together {framing}. Your work as {role}{company} "
            f"is exactly the profile we want in the room.\n\n"
            f"Open to a quick look at the details?"
        )
    elif slot == "gap_fill":
        body = (
            f"Hi {first_name},\n\n"
            f"We're rounding out the guest list for {framing}. Your background "
            f"in {role}{company} fills a specific gap we have right now.\n\n"
            f"Want me to send the brief?"
        )
    else:  # reminder
        body = (
            f"Hi {first_name},\n\n"
            f"Circling back on {framing} : just making sure this didn't get "
            f"lost. If timing isn't right, no worries : I can close the loop. "
            f"Otherwise happy to share details."
        )
    return {
        "subject": _default_subject(event, slot),
        "body": body,
        "method": "template",
        "slot": slot,
    }
