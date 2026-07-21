"""agents/relationship/enrichment_cache.py : identity-key kernel for the
enrichment cache.

Formerly ``backend/triage/enrichment_cache.py``. When the events-side triage
pipeline was retired, the relationship spine kept depending on this module's
identity-keying logic (every sync path — email, LinkedIn chat, Google contacts,
WhatsApp, spine dedup — keys people by it), so the kernel moved here and the
triage-only DB read/write half (``cache_get``/``cache_put``, which rehydrated
the triage ``RawEvidence`` shape) was dropped with the pipeline. The backing
table (``models.TriageEnrichmentCache``) and its data are preserved.

DESIGN
  - STRONG KEYS ONLY. A key is a LinkedIn slug ("li:<slug>"), a salted email
    hash ("em:<sha256>"), or a salted phone hash ("ph:<sha256>"). Never a name —
    a name-only key collides across people (the Brittany/Kyndred class of bug).
    ``identity_keys`` returns every strong key derivable from the inputs,
    strongest first, so the same person is matched whichever identifier a
    caller happens to know.
  - EMAIL-FIRST IS THE POINT. The email hash is free on every contact row; the
    LinkedIn slug costs a people-search to obtain. So an email match avoids the
    search action too, not just the profile fetch.
"""
from __future__ import annotations

import hashlib
import os

# Stable per-deploy salt for the email/phone hashes. It only needs to be
# deterministic across runs (so the same email maps to the same key); a fixed
# default is fine, override via env if you want to rotate.
_SALT = (os.environ.get("TRIAGE_CACHE_SALT") or "surplus-triage-enrich-v1").strip()

# ── LinkedIn URL helpers ───────────────────────────────────────────────────

# Locale/path suffixes LinkedIn appends after the public identifier. A naive
# "last path segment" parse turns .../in/yahal/en into the slug 'en' (→ 422/404).
_LINKEDIN_URL_SUFFIXES: frozenset[str] = frozenset({
    "en", "de", "fr", "es", "it", "pt", "nl", "zh", "ja", "ko", "ru",
    "detail", "overlay", "recent-activity", "details",
})


def _linkedin_slug(linkedin_url: str) -> str:
    """Extract the public-identifier slug from a LinkedIn profile URL.

    Robust to trailing slashes, query strings, and locale/path suffixes
    (.../in/yahal/en → 'yahal', not 'en'). Prefers the segment immediately
    after '/in/'; falls back to the last meaningful segment otherwise."""
    raw = (linkedin_url or "").split("?")[0].split("#")[0].rstrip("/")
    if not raw:
        return ""
    segments = [s for s in raw.split("/") if s]
    if "in" in segments:
        i = segments.index("in")
        if i + 1 < len(segments):
            return segments[i + 1]
    # No '/in/' marker: walk back from the end past known locale/path suffixes.
    for seg in reversed(segments):
        if seg.lower() not in _LINKEDIN_URL_SUFFIXES:
            return seg
    return segments[-1] if segments else ""


def _email_hash(email: str) -> str:
    """Salted sha256 of a lowercased real email, truncated. '' for blank/free
    addresses we can't key on confidently.

    We require a real (non-free) mailbox domain — a gmail/outlook address is a
    fine *key* on its own (the full address is unique to a person), so unlike the
    company-domain logic we DO key on free-provider emails here; we only drop
    addresses with no '@' at all."""
    e = (email or "").strip().lower()
    if not e or "@" not in e or e.startswith("@") or e.endswith("@"):
        return ""
    return hashlib.sha256((_SALT + "|" + e).encode("utf-8")).hexdigest()[:40]


def _phone_hash(phone: str) -> str:
    """Salted sha256 of a normalized phone number. We key on the LAST 10 digits so
    a country-code'd form (+1 415 555 1234) and a national one (415-555-1234) match
    for the common case. '' when we can't key confidently (< 10 digits). The '|ph|'
    namespace keeps phone hashes from ever colliding with email hashes."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return hashlib.sha256((_SALT + "|ph|" + digits[-10:]).encode("utf-8")).hexdigest()[:40]


def identity_keys(*, email: str = "", linkedin_url: str = "",
                  phone: str = "") -> list[str]:
    """Every STRONG cache key derivable from these inputs, strongest first
    (li > em > ph). Returns [] when none is available (we never key on a weak
    signal). Order matters: callers read in this order and stop at the first fresh
    hit, so the strongest identity wins. `phone` powers WhatsApp/SMS contacts, which
    have no email/LinkedIn to key on."""
    keys: list[str] = []
    slug = _linkedin_slug(linkedin_url) if linkedin_url else ""
    if slug:
        keys.append("li:" + slug.strip().lower())
    eh = _email_hash(email)
    if eh:
        keys.append("em:" + eh)
    ph = _phone_hash(phone)
    if ph:
        keys.append("ph:" + ph)
    return keys
