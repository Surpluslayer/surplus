"""pipeline/context/extract.py : mine durable FACTS from conversation content.

The scrape already yields company/title/about, and summary.py distills a thread
SUMMARY -- but the message CONTENT (what they're building, their interests, where
they're based, life events they mention) isn't captured as structured facts. This
reads a contact's thread and extracts ATTRIBUTE facts into the store, so the composer
has specific, durable context ACROSS sessions, not just this session's thread. That's
the lever on the eval's weak dimension (specificity).

Capturing context only: it WRITES facts, it does not touch the messaging harness.
"""
from __future__ import annotations

from ...spine import relationships
from ...spine.memory import upsert_fact
from .gather import thread_from_timeline

# Durable ATTRIBUTE keys we mine from messages. company/title ride the who-line and
# META keys (channel_preference, ...) are handled elsewhere; next_step is the harness's
# reconcile, so we deliberately DON'T emit it here.
_ALLOWED_KEYS = {"works_on", "interest", "based_in", "life_event", "role_detail"}

_SYSTEM = (
    "You read a conversation between a host and a CONTACT and extract durable facts "
    "ABOUT THE CONTACT (never about the host). Only facts clearly stated or strongly "
    "implied in the messages -- no guessing, no generic filler, no pleasantries or "
    "logistics. Each fact:\n"
    "  key: one of works_on | interest | based_in | life_event | role_detail\n"
    "  value: <= 12 words, specific\n"
    "  confidence: high (explicitly stated) | low (implied)\n"
    "Return ONLY JSON: {\"facts\":[{\"key\":\"...\",\"value\":\"...\",\"confidence\":\"...\"}]}"
)


def _slug(value: str) -> str:
    keep = "".join(c for c in (value or "").lower() if c.isalnum() or c == " ")
    return "-".join(keep.split())[:40]


def extract_facts(thread: list, *, contact_name: str = "") -> list:
    """LLM-extract durable contact facts from a thread. Returns [] when there's no
    LLM, an empty thread, or nothing worth keeping. Pure read -- no DB."""
    msgs = [m for m in (thread or []) if (m.get("text") or "").strip()]
    if not msgs:
        return []
    try:
        from ...book import _anthropic_available, _llm_json
    except Exception:  # noqa: BLE001
        return []
    if not _anthropic_available():
        return []
    convo = "\n".join(f"{m.get('who', '?')}: {(m.get('text') or '')[:400]}"
                      for m in msgs[-20:])
    out = _llm_json(_SYSTEM, f"Contact: {contact_name}\n\nConversation (oldest first):\n{convo}",
                    max_tokens=320, cheap=True) or {}
    facts = []
    for f in (out.get("facts") or []):
        key = (f.get("key") or "").strip().lower()
        value = (f.get("value") or "").strip()
        if key not in _ALLOWED_KEYS or not value:
            continue
        conf = "high" if (f.get("confidence") or "").lower() == "high" else "low"
        facts.append({"key": key, "value": value[:240], "confidence": conf,
                      "dedup_key": _slug(value)})
    return facts


def ingest_contact_facts(db, contact, *, commit: bool = True) -> dict:
    """Mine the contact's thread and upsert the extracted facts (source='message').
    Idempotent -- upsert keyed on (contact, key, dedup_key), so re-running refreshes
    in place instead of stacking. Best-effort: never raises; reports a count or error."""
    try:
        thread = thread_from_timeline(relationships.contact_timeline(db, contact))
        facts = extract_facts(thread, contact_name=getattr(contact, "name", "") or "")
        for f in facts:
            upsert_fact(db, contact.user_id, contact.id, f["key"], f["value"],
                        source="message", confidence=f["confidence"],
                        dedup_key=f["dedup_key"], commit=False)
        if commit:
            db.commit()
        return {"contact_id": contact.id, "extracted": len(facts),
                "keys": sorted({f["key"] for f in facts})}
    except Exception as exc:  # noqa: BLE001 : ingestion must never break a sweep
        return {"contact_id": getattr(contact, "id", None), "extracted": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def ingest_sweep(db, user_id: int, *, limit: int = 60) -> dict:
    """Ingest message facts for one user's contacts. Cost-bounded: a contact with no
    conversation thread is a no-op (extract makes NO LLM call), so spend tracks the
    message-active contacts, not the roster size. Per-contact best-effort.

    Takes a session (caller owns it). Whether this runs on a schedule is an opt-in
    activation decision -- see the module note -- because it's per-contact LLM cost."""
    try:
        contacts = relationships.list_contacts(db, user_id)[:max(1, limit)]
    except Exception:  # noqa: BLE001
        return {"contacts": 0, "with_facts": 0, "extracted": 0}
    extracted = touched = 0
    for c in contacts:
        r = ingest_contact_facts(db, c, commit=True)
        if r.get("extracted"):
            extracted += r["extracted"]
            touched += 1
    return {"contacts": len(contacts), "with_facts": touched, "extracted": extracted}
