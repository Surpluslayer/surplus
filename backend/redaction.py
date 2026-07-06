"""redaction.py : PII minimization before content leaves for the LLM.

The relationship pipeline sends real correspondence (thread bodies, notes,
update text) to Anthropic to compose messages. A follow-up almost never needs
to reproduce a raw email address, phone number, SSN, or card number — so we
strip those high-sensitivity identifiers from the model INPUT while leaving the
substance (names, roles, companies, topics, URLs, dates) intact. This is the
"send only what the task needs" minimization the security checklist calls for
(Phase 2, app-side controls) and shrinks the PII surface disclosed to the
subprocessor.

Scope + safety:
  - Only four conservative patterns are rewritten: email, phone, US SSN, and
    Luhn-valid card numbers. Everything else passes through untouched, so names
    and topical content the composer relies on are preserved.
  - Applied at the LLM-facing context transforms (`as_composer_context` /
    `as_agent_context`) and the inbound-reply classifier — never to the copies
    of a thread shown back to the host in the UI.
  - ON by default; set `SURPLUS_LLM_REDACTION=0` to disable (e.g. to A/B a
    messaging-quality eval). Redaction changes what the model reads, so run
    `python -m backend.agents.messaging_eval` before/after when tuning.

This is a minimization control, not anonymization: names and relationship
context still go to the provider. It complements, and does not replace, the DPA
/ no-training / ZDR provider terms.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Email: standard, low false-positive.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# US SSN: strict dashed form only (avoids mangling arbitrary 9-digit numbers).
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Phone: the classic 3-3-4 grouping (optional +country code, optional parens on
# the area code). Requiring this specific shape keeps false positives low —
# year ranges ("2020-2024"), IDs, and card fragments (4-4-4-4) don't match.
# Conservative by design: bare separator-less runs ("4155550132") are left
# alone rather than risk mangling non-phone numbers.
_PHONE = re.compile(
    r"(?<!\w)"
    r"(?:\+\d{1,3}[\s.\-]?)?"                 # optional country code
    r"(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"  # (area) prefix line
    r"(?!\w)"
)
# Candidate card runs (13-19 digits, optional space/dash grouping); Luhn-gated
# below so long non-card numbers survive.
_CARD_CANDIDATE = re.compile(r"(?<!\w)(?:\d[ \-]?){13,19}(?!\w)")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[card]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)
    return _CARD_CANDIDATE.sub(repl, text)


def enabled() -> bool:
    """Redaction is on unless SURPLUS_LLM_REDACTION is an explicit off value."""
    return (os.environ.get("SURPLUS_LLM_REDACTION") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def scrub_pii(text: str) -> str:
    """Replace email / phone / SSN / card identifiers with typed placeholders.
    Returns the text unchanged when redaction is disabled or there's nothing to
    scrub. Order matters: SSN before phone (both are dashed digit groups)."""
    if not text or not enabled() or not isinstance(text, str):
        return text
    text = _EMAIL.sub("[email]", text)
    text = _SSN.sub("[ssn]", text)
    text = _redact_cards(text)
    text = _PHONE.sub("[phone]", text)
    return text


def scrub_obj(obj: Any) -> Any:
    """Deep-copy a JSON-ish structure (dict/list/str), scrubbing every string.
    Non-strings pass through untouched. Used to minimize an assembled LLM
    context dict in one pass — only identifier substrings change, so names,
    URLs, dates, and topical content are preserved."""
    if not enabled():
        return obj
    if isinstance(obj, str):
        return scrub_pii(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj
