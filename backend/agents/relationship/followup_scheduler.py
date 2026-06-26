"""
agents/followup_scheduler.py : the "Gmail Schedule Send" layer for follow-ups.

When a first DM goes out we don't fire a follow-up on a fixed global timer
anymore. Instead we STAGE one: draft a context-aware follow-up, pick a
sensible default send time, and write a ScheduledFollowup row the host can
review, edit, reschedule, or cancel. The dispatch cron (admin.run-followups)
later sends every row whose send_at has arrived and is still `scheduled`.

Three pieces live here:

  compose_followup_text(prospect, event) -> str
      The draft. Calls Claude (Haiku) for a personalized nudge grounded in
      the recipient's role/company/what-they-work-on and the event framing,
      and falls back to the deterministic template (outreach.compose_followup)
      on any LLM failure so staging can never be blocked by a model outage.

  suggest_send_time(after=?) -> datetime
      The default fire time: now + FOLLOWUP_DELAY_HOURS, nudged out of
      weekends and into a daytime window. The host overrides this freely in
      the UI; it's only a sensible starting point.

  stage_followup(db, prospect, ...) -> ScheduledFollowup | None
      Idempotent writer: at most one pending (status="scheduled") row per
      prospect. Called right after a first DM is sent. Fail-soft : a staging
      failure must never break the send that triggered it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from ... import config, models
from ...jsonx import extract_json
from ...providers.base import strip_call_asks, strip_em_dashes
from ..outreach import compose_followup as _template_followup


# ── draft composition ─────────────────────────────────────────────────────

_MODEL = os.environ.get("FOLLOWUP_COMPOSE_MODEL", "claude-haiku-4-5-20251001")
_TIMEOUT_S = float(os.environ.get("FOLLOWUP_COMPOSE_TIMEOUT", "30"))
_MAX_TOKENS = 500

_SYSTEM = """You are writing a single follow-up LinkedIn DM on behalf of the host.

CONTEXT
The host already sent this person a first message inviting them to an event, and the recipient has NOT replied. This is the one gentle nudge. It must be lighter than the first message: a brief check-in, never a re-pitch from scratch.

GROUND RULES
  - 2 to 4 short sentences. LinkedIn DMs are short.
  - Warm, direct, human. No buzzwords, no "just circling back" cliche openers unless they fit naturally, no "I wanted to follow up" filler stacked on filler.
  - Reference ONE real, specific thing about the recipient (their role, company, or what they work on) so it doesn't read like a generic blast. Only use facts given in the input : never invent a project, talk, or detail.
  - Give an explicit, low-pressure off-ramp ("no worries if the timing's off, just let me know" / "happy to close the loop if it's not a fit"). The nudge should feel respectful of their silence, not pushy.
  - The ask is always about the EVENT (sharing details / coming along) or staying in touch. NEVER propose a call, Zoom, phone, or any live meeting.
  - Don't use em-dashes. Don't say "as an AI". Don't apologize for reaching out.

OUTPUT FORMAT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "message": "string : the follow-up DM, ready to send"
}"""


_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        _CLIENT = Anthropic(max_retries=2)
    return _CLIENT


def _user_message(prospect, event, prior_message: Optional[str] = None) -> str:
    parts = ["EVENT",
             f"Format: {getattr(event, 'format', '') or 'event'}",
             f"City: {getattr(event, 'city', '') or ''}"]
    brief = (getattr(event, "brief", "") or "").strip()
    if brief:
        parts += ["", "WHAT THE HOST SAID ABOUT THE EVENT (their own words):", brief]

    parts += ["", "RECIPIENT", f"Name: {prospect.name}",
              f"Role: {prospect.role}", f"Company: {prospect.company}"]
    if getattr(prospect, "headline", None):
        parts.append(f"Headline: {prospect.headline}")
    works_on = (getattr(prospect, "works_on", "") or "").strip()
    if works_on:
        parts.append(f"What they work on: {works_on}")

    prior = (prior_message or "").strip()
    if prior:
        parts += ["", "YOUR FIRST MESSAGE (already sent, they have NOT replied):",
                  prior,
                  "Write the nudge as a continuation of THIS message : reference "
                  "what you already said, do not repeat it or re-pitch from scratch."]
    parts += ["", "Write the JSON follow-up now."]
    return "\n".join(parts)


def _compose_via_claude(prospect, event,
                        prior_message: Optional[str] = None) -> Optional[str]:
    """One Haiku call. Returns the follow-up text or None on any failure."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return None
    try:
        resp = _client().messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            timeout=_TIMEOUT_S,
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[
                {"role": "user",
                 "content": _user_message(prospect, event, prior_message)},
                {"role": "assistant", "content": "{"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [followup compose] Claude failed: {type(exc).__name__}: {exc}")
        return None

    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    parsed = extract_json("{" + "\n".join(text_chunks))
    if not parsed:
        return None
    message = (parsed.get("message") or "").strip()
    return message or None


def compose_followup_text(prospect, event,
                          prior_message: Optional[str] = None) -> str:
    """The staged follow-up draft : LLM-composed, template fallback, cleaned.

    `prior_message` is the first DM already sent (its OutreachLog body) so the
    nudge builds on the actual conversation instead of re-deriving it from the
    prospect/event fields. The LLM uses the full text; the deterministic
    template only uses its presence to pick a "circling back" opener.

    Set FOLLOWUP_COMPOSE_DISABLE=1 to always use the deterministic template
    (escape hatch for cost spikes / model issues), matching outreach.compose.
    """
    disabled = (os.environ.get("FOLLOWUP_COMPOSE_DISABLE") or "").strip().lower()
    if disabled not in ("", "0", "false", "no"):
        text = _template_followup(prospect, event, prior_message)
    else:
        text = (_compose_via_claude(prospect, event, prior_message)
                or _template_followup(prospect, event, prior_message))
    # Same outbound hygiene the send-gate applies, so the staged preview ==
    # what gets transmitted: strip call asks + dashes LinkedIn mangles.
    return (strip_call_asks(strip_em_dashes(text)) or "").strip()


# ── send-time suggestion ───────────────────────────────────────────────────

# Daytime window (UTC) the default suggestion is clamped into. A follow-up
# landing at 3am reads as automated; nudging it into business-ish hours keeps
# it human. This is a heuristic starting point only : the host reschedules
# freely in the UI, so we don't try to be timezone-perfect here.
_DAY_START_H = 9
_DAY_END_H = 18


def suggest_send_time(after: Optional[datetime] = None) -> datetime:
    """Default fire time for a staged follow-up: `after` + FOLLOWUP_DELAY_HOURS,
    pushed off weekends and into the daytime window. Timezone-aware UTC."""
    base = (after or datetime.now(timezone.utc)) + timedelta(
        hours=config.FOLLOWUP_DELAY_HOURS)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)

    # Clamp into the daytime window.
    if base.hour < _DAY_START_H:
        base = base.replace(hour=_DAY_START_H, minute=0, second=0, microsecond=0)
    elif base.hour >= _DAY_END_H:
        base = (base + timedelta(days=1)).replace(
            hour=_DAY_START_H, minute=0, second=0, microsecond=0)

    # Skip weekends (Mon=0 .. Sun=6): Sat/Sun -> next Monday morning.
    if base.weekday() >= 5:
        base = (base + timedelta(days=7 - base.weekday())).replace(
            hour=_DAY_START_H, minute=0, second=0, microsecond=0)
    return base


# ── staging ────────────────────────────────────────────────────────────────

def pending_followup(db, prospect_id: int) -> Optional[models.ScheduledFollowup]:
    """The one pending (scheduled) follow-up for this prospect, if any."""
    return (db.query(models.ScheduledFollowup)
              .filter(models.ScheduledFollowup.prospect_id == prospect_id,
                      models.ScheduledFollowup.status == "scheduled")
              .first())


def _last_sent_message(db, prospect_id: int) -> Optional[str]:
    """The body of the most recent DM actually sent to this prospect : the
    first message the follow-up is nudging on. Used to ground composition in
    the real conversation rather than re-deriving from prospect/event fields."""
    log = (db.query(models.OutreachLog)
             .filter(models.OutreachLog.prospect_id == prospect_id,
                     models.OutreachLog.state.in_(("message_sent",
                                                   "follow_up_sent")))
             .order_by(models.OutreachLog.ts.desc())
             .first())
    return (log.body or "").strip() if log is not None else None


def stage_followup(
    db,
    prospect: models.Prospect,
    *,
    commit: bool = True,
) -> Optional[models.ScheduledFollowup]:
    """Stage a context-drafted follow-up for `prospect` at a suggested time.

    Always drafts + stages (a follow-up is created for every first DM) : the
    draft is the product. Whether it actually *sends* is the host's call,
    gated at dispatch by User.auto_followups_enabled (off -> the row waits in
    the queue for a manual send-now; on -> the cron sends it at send_at).

    Idempotent: returns the existing pending row untouched if one already
    exists (so re-sending a first DM, or a retried webhook, can't stack
    duplicates). Returns None and swallows any error : staging must never
    break the send that triggered it.
    """
    try:
        event = prospect.event
        if event is None:
            return None

        existing = pending_followup(db, prospect.id)
        if existing is not None:
            return existing

        prior = _last_sent_message(db, prospect.id)
        body = compose_followup_text(prospect, event, prior)
        if not body:
            return None

        send_at = suggest_send_time()
        row = models.ScheduledFollowup(
            prospect_id=prospect.id,
            body=body,
            send_at=send_at,
            suggested_send_at=send_at,
            status="scheduled",
        )
        db.add(row)
        if commit:
            db.commit()
            db.refresh(row)
        return row
    except Exception as exc:  # noqa: BLE001
        print(f"  [stage_followup] {getattr(prospect, 'id', '?')}: "
              f"{type(exc).__name__}: {exc}")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def cancel_pending_followups(
    db,
    prospect_id: int,
    *,
    reason: str = "user",
    commit: bool = True,
) -> int:
    """Cancel every pending follow-up for a prospect. Returns count cancelled.

    Called when a prospect replies (reason="replied") so we never nudge
    someone who already engaged, and by the manual cancel route (reason="user").
    """
    rows = (db.query(models.ScheduledFollowup)
              .filter(models.ScheduledFollowup.prospect_id == prospect_id,
                      models.ScheduledFollowup.status == "scheduled")
              .all())
    now = datetime.now(timezone.utc)
    for r in rows:
        r.status = "cancelled"
        r.cancel_reason = reason
        r.updated_at = now
    if rows and commit:
        db.commit()
    return len(rows)
