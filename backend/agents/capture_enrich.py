"""
agents/capture_enrich.py : prompt 5 — capture enrichment (on add).

Runs ONCE at capture, before the person lands in the book : turns the raw
capture input (QR/badge payload, pasted LinkedIn URL, typed name + whatever
the operator added) into a clean {name, title, firm, linkedin, email, met_at}
record, so a fresh capture never renders as "Unknown" / a bare handle.

Reuse over new code:
  - book._llm_json for the Claude call : same claude-sonnet-4-6 model, gated on
    ANTHROPIC_API_KEY, returns None on ANY failure so callers always have the
    deterministic path.
  - Without a key (or on any LLM failure) a handle heuristic still recovers
    "Satya Nadella" from satya-nadella : extraction, not guessing.

Conservative by design : extraction only (the prompt forbids guessing; the
heuristic only splits the vanity handle), and APPLY only fills fields that are
empty / placeholder ("Unknown" / the bare handle). Operator-entered values are
never overwritten and existing values are never blanked.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Placeholders that mean "we know nothing" : safe to overwrite with enrichment.
_PLACEHOLDERS = {"", "unknown", "general"}

_ENRICH_SYSTEM = (
    "Turn a raw captured contact into a clean record. Input may be a scanned "
    "badge payload, a LinkedIn URL's page text, or free text.\n\n"
    "Extract only what is clearly present; do not guess. A LinkedIn vanity "
    "handle like 'satya-nadella' or 'satyanadella' clearly spells the person's "
    "name : title-case it. Leave unknown fields null.\n\n"
    "Return ONLY JSON:\n"
    "{\n"
    '  "name": "<full name or null>",\n'
    '  "title": "<role or null>",\n'
    '  "firm": "<company / firm or null>",\n'
    '  "linkedin": "<url or null>",\n'
    '  "email": "<email or null>",\n'
    '  "met_at": "<event name>"\n'
    "}"
)


def _clean(val: Any) -> Optional[str]:
    s = (str(val) if val is not None else "").strip()
    return s or None


def _is_placeholder(val: Any, handle: str = "") -> bool:
    """True when a stored field carries no real information : empty, the
    schema default 'Unknown', or just the LinkedIn handle echoed back."""
    s = (_clean(val) or "").lower()
    if s in _PLACEHOLDERS:
        return True
    h = (handle or "").strip().lower()
    return bool(h) and s in {h, h.replace("-", " "), h.replace("-", "")}


def _handle_from_url(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def name_from_handle(handle: str) -> Optional[str]:
    """Recover a display name from a LinkedIn vanity handle, extraction-only:
    'satya-nadella-b123' -> 'Satya Nadella'. Needs at least two alphabetic
    parts (a lone token like 'satyanadella' can't be split without guessing,
    so we return None and leave it to the LLM path)."""
    parts = [p for p in re.split(r"[-_.]+", (handle or "").strip()) if p]
    # Drop trailing LinkedIn dedup junk: pure digits or short hex-ish blobs.
    while parts and (parts[-1].isdigit()
                     or (len(parts[-1]) <= 7
                         and re.fullmatch(r"[0-9]+[a-z0-9]*|[a-z]?[0-9][a-z0-9]*",
                                          parts[-1].lower()))):
        parts.pop()
    words = [p for p in parts if p.isalpha()]
    if len(words) < 2 or len(words) != len(parts):
        return None
    return " ".join(w.capitalize() for w in words)[:120]


def _raw_capture_text(prospect: Any) -> str:
    """The raw capture payload as one text block : URL + handle + whatever the
    operator typed. This is ALL first-party input — nothing fetched."""
    handle = _handle_from_url(getattr(prospect, "linkedin_url", "") or "")
    lines = [
        f"linkedin_url: {getattr(prospect, 'linkedin_url', None) or ''}",
        f"linkedin_handle: {handle}",
    ]
    for label, attr in (("entered_name", "name"), ("entered_role", "role"),
                        ("entered_company", "company"), ("email", "email"),
                        ("note", "note")):
        v = _clean(getattr(prospect, attr, None))
        if v and not _is_placeholder(v, handle if attr == "name" else ""):
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def build_record(prospect: Any, event: Any) -> dict:
    """Prompt 5 : the structured record for one capture. LLM when a key is
    set, the handle heuristic otherwise (or whenever the LLM path fails).
    Never raises, never returns guessed values."""
    event_name = (_clean(getattr(event, "label", None))
                  or _clean(getattr(event, "event_name", None)) or "")
    handle = _handle_from_url(getattr(prospect, "linkedin_url", "") or "")

    record: dict = {"name": None, "title": None, "firm": None,
                    "linkedin": _clean(getattr(prospect, "linkedin_url", None)),
                    "email": None, "met_at": event_name}

    from .book import _llm_json  # gated on ANTHROPIC_API_KEY; None on failure
    out = _llm_json(
        _ENRICH_SYSTEM,
        f"Raw input:\n{_raw_capture_text(prospect)}\n\n"
        f"Captured at event: {event_name}",
        max_tokens=300,
    )
    if isinstance(out, dict):
        for k in ("name", "title", "firm", "email"):
            v = _clean(out.get(k))
            if v and v.lower() not in {"null", "none", "unknown"}:
                record[k] = v

    # Heuristic floor : with or without the LLM, the vanity handle alone
    # usually spells the name.
    if record["name"] is None:
        record["name"] = name_from_handle(handle)
    return record


def apply_record(prospect: Any, record: dict) -> bool:
    """Fill the Prospect's placeholder fields from an enrichment record.
    Fill-only : operator-entered values and prior enrichment are never
    overwritten, nothing is ever blanked. Returns True if anything changed."""
    handle = _handle_from_url(getattr(prospect, "linkedin_url", "") or "")
    changed = False

    name = _clean(record.get("name"))
    if name and _is_placeholder(getattr(prospect, "name", None), handle):
        prospect.name = name[:120]
        changed = True

    title = _clean(record.get("title"))
    if title:
        if _is_placeholder(getattr(prospect, "role", None)):
            prospect.role = title[:300]
            changed = True
        # The Book surface titles people from `headline` : mirror it there
        # when no real headline exists yet.
        if _is_placeholder(getattr(prospect, "headline", None)):
            prospect.headline = title[:300]
            changed = True

    firm = _clean(record.get("firm"))
    if firm and _is_placeholder(getattr(prospect, "company", None)):
        prospect.company = firm[:120]
        changed = True

    email = _clean(record.get("email"))
    if email and "@" in email and not _clean(getattr(prospect, "email", None)):
        prospect.email = email.lower()
        changed = True

    return changed


def enrich_capture(prospect: Any, event: Any) -> Optional[dict]:
    """The one call sites use : build the prompt-5 record and apply it to the
    Prospect row (in place, uncommitted : the caller owns the transaction).
    Best-effort throughout — returns the record, or None when enrichment
    failed or had nothing to add."""
    try:
        record = build_record(prospect, event)
        apply_record(prospect, record)
        return record
    except Exception:  # noqa: BLE001 : enrichment must never break a capture
        return None


def refresh_contact(db, contact: Any, prospect: Any) -> None:
    """Back-fill the durable Contact when its stored name/company are still
    placeholders (e.g. it was created from a pre-enrichment capture of the
    same person). Fill-only + fail-soft, mirroring link_contact's posture."""
    if contact is None:
        return
    try:
        handle = _handle_from_url(getattr(contact, "linkedin_url", None)
                                  or getattr(prospect, "linkedin_url", "") or "")
        changed = False
        name = _clean(getattr(prospect, "name", None))
        if (name and not _is_placeholder(name, handle)
                and _is_placeholder(getattr(contact, "name", None), handle)):
            contact.name = name
            changed = True
        company = _clean(getattr(prospect, "company", None))
        if (company and not _is_placeholder(company)
                and _is_placeholder(getattr(contact, "company", None))):
            contact.company = company
            changed = True
        if changed:
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
