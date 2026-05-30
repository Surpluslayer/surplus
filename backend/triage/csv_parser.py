"""
triage/csv_parser.py : flexible Luma CSV parser.

Luma CSVs vary wildly because hosts add custom questions per-event
('Do you use Stripe?', 'Are you a creator?', 'Company stage', etc.).
The parser:

  1. Maps known headers to canonical fields (case-insensitive, fuzzy)
  2. Preserves everything unknown in raw_application_data so the scoring
     step in PR C can see the custom answers when judging fit
  3. Skips empty rows + rows with no email / no name (clearly bad data)

Returns a list of normalized dicts ready to insert as Applicant rows.
"""
from __future__ import annotations
import csv
import io
import json
import re
from typing import Iterable


# Canonical fields the rest of the triage pipeline expects on an Applicant.
# Header variants (lowercased, stripped) → canonical field name. The mapping
# is intentionally fuzzy : Luma exports have changed over the years and hosts
# rename fields when they edit the form.
_HEADER_MAP: dict[str, str] = {
    # name
    "name": "name",
    "full name": "name",
    "your name": "name",
    "first and last name": "name",
    "attendee name": "name",
    # email
    "email": "email",
    "email address": "email",
    "contact email": "email",
    "your email": "email",
    # role / title
    "role": "role",
    "title": "role",
    "job title": "role",
    "your role": "role",
    "what's your role": "role",
    "what is your role": "role",
    "what do you do": "role",
    "position": "role",
    # company
    "company": "company",
    "company name": "company",
    "where do you work": "company",
    "your company": "company",
    "organization": "company",
    "org": "company",
    "employer": "company",
    # website — note: bare "url" intentionally removed to avoid matching
    # Luma's qr_code_url system column. Use more specific keys only.
    "website": "website",
    "company website": "website",
    "your website": "website",
    "company url": "website",
    "product url": "website",
    "startup url": "website",
    # linkedin
    "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url",
    "linkedin profile": "linkedin_url",
    "linkedin profile url": "linkedin_url",
    "linkedin link": "linkedin_url",
    "your linkedin": "linkedin_url",
    "linkedin username": "linkedin_url",
}

# Luma system columns that look like they could match canonical fields via
# substring but must never be treated as applicant data.
_SYSTEM_COLUMN_PREFIXES: tuple[str, ...] = (
    "qr code",       # qr_code_url
    "qr_code",
    "guest id",
    "ticket type",
    "ticket name",
    "checked in",
    "approval status",
    "utm ",
    "amount",
    "currency",
    "coupon",
    "eth address",
    "solana address",
    "survey response",
    "instagram",     # instagram username ≠ name
    "twitter",       # twitter handle ≠ name
    "facebook",
)

CANONICAL_FIELDS: tuple[str, ...] = (
    "name", "email", "role", "company", "website", "linkedin_url",
)


def _normalize_header(h: str) -> str:
    """Strip surrounding whitespace, lowercase, collapse internal punctuation
    so 'LinkedIn URL?' and 'linkedin_url' both match."""
    h = (h or "").strip().lower()
    h = re.sub(r"[_:?!.\-/\\]+", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


_HEADER_MAP_SORTED = sorted(_HEADER_MAP.items(), key=lambda kv: -len(kv[0]))


def _resolve_field(header: str) -> str | None:
    """Return the canonical field name for `header`, or None if unknown."""
    norm = _normalize_header(header)
    # Block Luma system columns and known social/platform handles before
    # attempting any match — they look like canonical fields via substring
    # (e.g. qr_code_url → "url" → website; instagram username → "your name").
    for prefix in _SYSTEM_COLUMN_PREFIXES:
        if norm.startswith(prefix) or prefix in norm:
            return None
    if norm in _HEADER_MAP:
        return _HEADER_MAP[norm]
    # Substring fallback : iterate by key length descending so the more
    # specific match wins ('linkedin url' beats bare 'url' for headers
    # like 'LinkedIn Profile URL (optional)').
    for key, canonical in _HEADER_MAP_SORTED:
        if key in norm:
            return canonical
    return None


def _looks_like_linkedin(value: str) -> bool:
    return "linkedin.com" in (value or "").lower()


def _coerce_linkedin(value: str) -> str:
    """Accept handles ('daniel-wang') OR full URLs and normalize to a URL."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("http"):
        return v
    if _looks_like_linkedin(v):
        return v if v.startswith("//") else f"https://{v}"
    # Bare handle : assume linkedin.com/in/<handle>
    if re.match(r"^[A-Za-z0-9_-]+$", v):
        return f"https://www.linkedin.com/in/{v}"
    return v


def parse_csv(content: str | bytes) -> list[dict]:
    """Parse a Luma CSV into a list of normalized applicant dicts.

    Each dict has the canonical keys (name, email, role, company, website,
    linkedin_url) plus a `raw_application_data` dict holding every other
    column from the source row.

    Rejects rows with NO name AND NO email : those are clearly bad data
    (empty rows, separator rows, etc.).
    """
    if isinstance(content, bytes):
        # Luma exports UTF-8; BOM is occasional. Strip it.
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []

    # Build a header→canonical map up-front so we don't re-resolve per row.
    header_to_canonical: dict[str, str] = {}
    for h in reader.fieldnames:
        canonical = _resolve_field(h)
        if canonical:
            header_to_canonical[h] = canonical

    out: list[dict] = []
    for row in reader:
        canonical: dict[str, str] = {f: "" for f in CANONICAL_FIELDS}
        raw: dict[str, str] = {}
        for header, value in row.items():
            if header is None:
                continue
            cleaned = (value or "").strip()
            if header in header_to_canonical:
                field = header_to_canonical[header]
                # First non-empty wins : if two headers map to the same
                # canonical field, prefer the first one that has a value.
                if not canonical[field]:
                    canonical[field] = cleaned
            elif cleaned:
                raw[header] = cleaned

        if canonical["linkedin_url"]:
            canonical["linkedin_url"] = _coerce_linkedin(canonical["linkedin_url"])

        # Reject obviously empty rows : no name AND no email == skip.
        if not canonical["name"] and not canonical["email"]:
            continue

        applicant = dict(canonical)
        applicant["raw_application_data"] = raw
        out.append(applicant)
    return out


def parse_csv_file(file_obj) -> list[dict]:
    """Convenience wrapper for FastAPI's UploadFile.file : read the whole
    file into memory and parse. CSVs are small enough this is fine."""
    content = file_obj.read()
    return parse_csv(content)
