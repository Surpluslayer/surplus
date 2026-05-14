"""Load a CSV of event guests and normalize into Person records.

Handles Luma exports out of the box and any generic CSV via flexible column
matching. No LLM calls — pure parsing + rule-based normalization.

Usage:
    from backend.matching.ingest import load_csv
    people = load_csv("data/input/my_event.csv")
"""
from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from backend.matching.schema import Person, EXP_LEVELS


# Lower-cased column-name hints. For each Person field, we check the CSV
# headers in order; first hint that's a substring of a header wins.
COLUMN_HINTS: dict[str, list[str]] = {
    "name": ["name", "full name", "first_name"],
    "email": ["email"],
    "role": ["role"],
    "title": ["job title", "title", "position"],
    "company": ["company", "organization", "employer"],
    "linkedin_url": ["linkedin"],
    "x_handle": ["x (twitter)", "twitter", "x handle", " x "],
    "github_username": ["github"],
    "ticket_type": ["ticket_name", "ticket type", "ticket"],
    "exp_level": ["level of experience", "experience"],
    "checked_in": ["checked_in_at", "checked in"],
}


# Rules for bucketing free-text experience into canonical buckets.
# Order matters — first match wins. Higher levels checked first so "expert"
# beats "intermediate" even if both keywords appear.
EXP_LEVEL_RULES: list[tuple[str, list[str]]] = [
    ("expert",       ["expert", "senior", "principal", "staff engineer", "10+ year", "professional"]),
    ("advanced",     ["advanced", "5 year", "5+ year", "5years", "lead", "extensive"]),
    ("intermediate", ["intermediate", "moderate", "some", "1 year", "2 year", "3 year", "1-2", "medium"]),
    ("beginner",     ["beginner", "beginer", "entry", "basic", "novice", "new", "starting", "none", "no experience", "0", "na"]),
]


def load_csv(path: str | Path) -> list[Person]:
    """Read a CSV and return Person records.

    Filters:
      - drops rows with no name and no contact handles (linkedin/x/github)
      - deduplicates by stable person id (name + linkedin + email hash)
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        col_map = _build_column_map(headers)
        seen_ids: set[str] = set()
        people: list[Person] = []
        for row in reader:
            person = _row_to_person(row, col_map)
            if person is None:
                continue
            if person.id in seen_ids:
                continue  # CSV duplicate — already loaded this person
            seen_ids.add(person.id)
            people.append(person)
    return people


def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Map our Person field names -> actual CSV header strings.

    Uses substring matching against COLUMN_HINTS. Returns only fields where
    a header matched; downstream code treats missing as empty default.
    """
    headers_lc = [(h, h.lower()) for h in headers]
    mapping: dict[str, str] = {}
    for field, hints in COLUMN_HINTS.items():
        for hint in hints:
            match = next((h for h, lc in headers_lc if hint in lc), None)
            if match:
                mapping[field] = match
                break
    return mapping


def _row_to_person(row: dict[str, Any], col_map: dict[str, str]) -> Person | None:
    name = _get(row, col_map, "name").strip()
    if not name:
        return None

    linkedin = _normalize_url(_get(row, col_map, "linkedin_url"))
    x_handle = _extract_x_handle(_get(row, col_map, "x_handle"))
    github_username = _extract_github_username(_get(row, col_map, "github_username"))

    # Drop rows with no contact handles at all — nothing to enrich from
    if not (linkedin or x_handle or github_username):
        return None

    email = _get(row, col_map, "email").strip().lower()
    role = _get(row, col_map, "role").strip()
    title = _get(row, col_map, "title").strip()
    company = _get(row, col_map, "company").strip()
    ticket_type = _get(row, col_map, "ticket_type").strip() or "unknown"
    exp_raw = _get(row, col_map, "exp_level")
    exp_level = _normalize_exp_level(exp_raw)
    checked_in = bool(_get(row, col_map, "checked_in").strip())

    person_id = _stable_id(name, linkedin, email)

    return Person(
        id=person_id,
        name=name,
        email=email,
        role=role,
        title=title,
        company=company,
        linkedin_url=linkedin,
        x_handle=x_handle,
        github_username=github_username,
        ticket_type=ticket_type,
        exp_level=exp_level,
        checked_in=checked_in,
        raw_row=row,
    )


def _get(row: dict[str, Any], col_map: dict[str, str], field: str) -> str:
    header = col_map.get(field)
    if not header:
        return ""
    value = row.get(header, "")
    return value if value is not None else ""


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    # Strip trailing slashes and query strings
    url = url.rstrip("/").split("?")[0]
    # Ensure scheme
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_x_handle(value: str) -> str:
    """Return a bare handle like 'corylevy' from URL or '@handle' input."""
    v = value.strip()
    if not v:
        return ""
    # Match x.com/<handle> or twitter.com/<handle>
    m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,30})", v, re.IGNORECASE)
    if m:
        return m.group(1)
    # Match @handle
    if v.startswith("@"):
        return v[1:].split()[0].split("/")[0]
    # Already a bare handle
    if re.fullmatch(r"[A-Za-z0-9_]{1,30}", v):
        return v
    return ""


def _extract_github_username(value: str) -> str:
    v = value.strip()
    if not v:
        return ""
    m = re.search(r"github\.com/([A-Za-z0-9-]{1,39})", v, re.IGNORECASE)
    if m:
        return m.group(1)
    if v.startswith("@"):
        v = v[1:]
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}", v):
        return v
    return ""


def _normalize_exp_level(text: str) -> str:
    """Bucket free-text experience answer into a canonical level.

    Returns one of EXP_LEVELS. Returns 'unknown' for empty or unparseable.
    """
    t = (text or "").strip().lower()
    if not t:
        return "unknown"
    for bucket, keywords in EXP_LEVEL_RULES:
        if any(kw in t for kw in keywords):
            return bucket
    return "unknown"


def _stable_id(name: str, linkedin: str, email: str) -> str:
    """Stable 12-char hash so the same person across re-runs gets the same id."""
    key = "|".join([name.lower().strip(), linkedin.lower(), email.lower()])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


# ---- Convenience helpers ----

def summarize(people: Iterable[Person]) -> dict[str, Any]:
    """One-glance stats — useful for sanity-checking an ingested CSV."""
    people = list(people)
    n = len(people)
    if n == 0:
        return {"total": 0}
    return {
        "total": n,
        "has_linkedin": sum(1 for p in people if p.linkedin_url),
        "has_x": sum(1 for p in people if p.x_handle),
        "has_github": sum(1 for p in people if p.github_username),
        "has_all_three": sum(1 for p in people if p.linkedin_url and p.x_handle and p.github_username),
        "ticket_types": _counter([p.ticket_type for p in people]),
        "exp_levels": _counter([p.exp_level for p in people]),
        "checked_in_count": sum(1 for p in people if p.checked_in),
    }


def _counter(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))
