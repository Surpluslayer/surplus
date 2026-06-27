"""Per-contact LLM rolling summaries for compressed thread context.

When a thread exceeds the recent window, older messages are summarized once via
Haiku and cached on ContactFact (key=thread_summary, dedup_key=content hash).
Each contact keeps their own summary; it is refreshed when the older slice changes.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from .reconcile import DEFAULT_RECENT_MESSAGES, summarize_older_thread

_SUMMARY_SYSTEM = (
    "You compress conversation history for a host's follow-up assistant.\n"
    "Return ONLY JSON: {\"summary\":\"...\"}\n\n"
    "Capture what was actually discussed, any open loops or host promises, "
    "relationship tone, and facts a follow-up writer must not forget. "
    "Use ONLY facts from the messages. Do not invent details. "
    "Plain prose, under 120 words, no bullets, no em dashes."
)

_THREAD_SUMMARY_LLM = os.environ.get("THREAD_SUMMARY_LLM", "1").strip().lower() in (
    "1", "true", "yes", "on")


def _fingerprint(older: list[dict], recent_limit: int) -> str:
    parts = [f"recent={recent_limit}"]
    for m in older or []:
        parts.append(
            f"{m.get('who')}|{m.get('when')}|{(m.get('text') or '')[:240]}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def _format_thread(msgs: list[dict]) -> str:
    lines: list[str] = []
    for m in msgs or []:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = m.get("who")
        label = "Host" if who == "host" else ("Contact" if who == "them" else "Context")
        lines.append(f"{label}: {text[:500]}")
    return "\n".join(lines)


def _cached_summary(db, contact_id: int, fingerprint: str) -> Optional[str]:
    from ...spine.memory import get_facts
    for row in get_facts(db, contact_id, key="thread_summary"):
        if row.dedup_key == fingerprint and (row.value or "").strip():
            return row.value.strip()
    return None


def _prior_summary(db, contact_id: int) -> Optional[str]:
    from ...spine.memory import get_facts
    rows = get_facts(db, contact_id, key="thread_summary")
    if rows and (rows[0].value or "").strip():
        return rows[0].value.strip()
    return None


def _prune_stale_summaries(db, contact_id: int, keep_fingerprint: str) -> None:
    from ..... import models
    (db.query(models.ContactFact)
     .filter(models.ContactFact.contact_id == contact_id,
             models.ContactFact.key == "thread_summary",
             models.ContactFact.dedup_key != keep_fingerprint)
     .delete(synchronize_session=False))


def _llm_summarize(
    older: list[dict],
    *,
    contact_name: str = "",
    prior_summary: str = "",
) -> Optional[str]:
    if not _THREAD_SUMMARY_LLM:
        return None
    from ...book import _anthropic_available, _llm_json
    if not _anthropic_available():
        return None
    name = (contact_name or "this contact").strip() or "this contact"
    user = f"Contact: {name}\n\n"
    if prior_summary.strip():
        user += (
            "Prior rolling summary (update and extend it from the messages below; "
            "keep what is still true, drop what is obsolete, add new facts):\n"
            f"{prior_summary.strip()}\n\n")
    user += f"Older messages to compress:\n{_format_thread(older)}"
    out = _llm_json(_SUMMARY_SYSTEM, user, max_tokens=220, cheap=True)
    summary = (out or {}).get("summary") if isinstance(out, dict) else None
    summary = (summary or "").strip()
    return summary or None


def summarize_older_messages(
    older: list[dict],
    *,
    recent_limit: int = DEFAULT_RECENT_MESSAGES,
    db=None,
    user_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    contact_name: str = "",
) -> Optional[str]:
    """LLM rolling summary with ContactFact cache; deterministic fallback."""
    if not older:
        return None

    fingerprint = _fingerprint(older, recent_limit)
    if db is not None and contact_id:
        hit = _cached_summary(db, contact_id, fingerprint)
        if hit:
            return f"Earlier conversation: {hit}"

    prior = _prior_summary(db, contact_id) if db and contact_id else ""
    summary = _llm_summarize(
        older, contact_name=contact_name, prior_summary=prior or "")
    if not summary:
        det = summarize_older_thread(older)
        return det or None

    wrapped = f"Earlier conversation: {summary}"
    if db is not None and user_id is not None and contact_id:
        from ...spine.memory import upsert_fact
        upsert_fact(
            db, user_id, contact_id, "thread_summary", summary,
            source="thread_compress", confidence="high",
            dedup_key=fingerprint, commit=False,
        )
        _prune_stale_summaries(db, contact_id, fingerprint)
    return wrapped


def window_and_summarize(
    prior_full: list[dict],
    recent: int = DEFAULT_RECENT_MESSAGES,
    *,
    db=None,
    user_id: Optional[int] = None,
    contact=None,
) -> tuple[list[dict], Optional[str]]:
    """Split thread into recent verbatim + LLM summary of older messages."""
    msgs = list(prior_full or [])
    limit = max(1, recent)
    if len(msgs) <= limit:
        return msgs, None
    older, recent_msgs = msgs[:-limit], msgs[-limit:]
    summary = summarize_older_messages(
        older,
        recent_limit=limit,
        db=db,
        user_id=user_id,
        contact_id=getattr(contact, "id", None),
        contact_name=(getattr(contact, "name", None) or ""),
    )
    return recent_msgs, summary
