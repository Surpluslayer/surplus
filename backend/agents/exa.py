"""
agents/exa.py — Exa-backed prospect discovery.

Same contract as `llm.discover_candidates(source, icp)` — returns a list of
candidate dicts in the per-source shape — but uses Exa's semantic search
instead of Claude + web_search. Cheaper, faster, and Exa's index has good
LinkedIn / GitHub / X coverage so we can extract the canonical profile URL
straight from the result without an extra parsing step.

Gated by EXA_API_KEY. When unset, callers fall back to llm.discover_candidates
(Claude) and ultimately the mock pool.

Result shapes per source — matching what the existing SourceAdapter expects:

  linkedin: {identity, name, linkedin_url, role?, company?, contact_resolved: True}
  github  : {identity, name, github_url, gh_stars: 0}
  x       : {identity, name, x_url, x_followers: 0}

The 0s for gh_stars / x_followers are because Exa's index returns metadata
about the page, not live API data. The scorer accepts 0 gracefully — the
prospect just won't get the signal bonus.
"""
from __future__ import annotations
import os
import re
from typing import Optional


def _api_key() -> str:
    """Read EXA_API_KEY and strip whitespace (same hardening as ANTHROPIC_API_KEY)."""
    return (os.environ.get("EXA_API_KEY") or "").strip()


def exa_available() -> bool:
    return bool(_api_key())


# Extract the handle from each platform's profile URL
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9_-]+)", re.I)
_GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9_-]+)/?$", re.I)
_X_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/?(?:$|\?)", re.I)

# Title parsing — LinkedIn page titles follow a consistent format
_LI_TITLE_RE = re.compile(r"^(.+?)\s*-\s*(.+?)\s*(?:\|\s*LinkedIn)?\s*$")


def discover_via_exa(source: str, icp: dict, max_candidates: int = 5) -> list[dict]:
    """
    Search Exa for one source's candidates matching the ICP.

    Uses Exa's `category` filter to scope to actual profile pages — this is
    more precise than `includeDomains` alone (which would also surface
    LinkedIn job posts, company pages, etc.). We pass both as belt-and-
    suspenders so we don't pay tokens reading pages we'll discard anyway.

    Returns up to `max_candidates` dicts. On any error, returns [] so the
    caller can fall through to another backend or the mock pool.
    """
    if not exa_available():
        return []
    if source not in ("linkedin", "github", "x"):
        return []

    query = _build_query(source, icp)
    domain = {
        "linkedin": "linkedin.com",
        "github": "github.com",
        "x": "x.com",
    }[source]
    # Exa's canonical category labels for entity-type results
    category = {
        "linkedin": "linkedin profile",
        "github": "github",
        "x": "tweet",
    }[source]
    body = {
        "query": query,
        "type": "neural",
        "category": category,
        # over-fetch — even with category filter, some results won't yield a
        # parseable handle (snippets, archives, etc.)
        "numResults": max(max_candidates * 3, 10),
        "includeDomains": [domain],
        "contents": {"text": True},
    }
    headers = {
        "x-api-key": _api_key(),
        "content-type": "application/json",
        "accept": "application/json",
    }

    try:
        import httpx
        with httpx.Client(timeout=20.0) as client:
            resp = client.post("https://api.exa.ai/search",
                               headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001
        print(f"  [exa] {source} search failed: {type(exc).__name__}: {exc}")
        return []
    if resp.status_code >= 400:
        print(f"  [exa] {source} search {resp.status_code}: {resp.text[:200]}")
        return []
    try:
        data = resp.json()
    except Exception:
        return []

    results = data.get("results") or []
    out: list[dict] = []
    seen_identities: set[str] = set()
    for r in results:
        cand = _parse_result(source, r)
        if cand is None:
            continue
        if cand["identity"] in seen_identities:
            continue
        seen_identities.add(cand["identity"])
        out.append(cand)
        if len(out) >= max_candidates:
            break
    return out


# ---- query construction --------------------------------------------------

def _build_query(source: str, icp: dict) -> str:
    """Compose a semantic query Exa can match. One sentence, plain English."""
    role = (icp.get("role") or "").strip()
    seniority = (icp.get("seniority") or "").strip()
    co_stage = (icp.get("co_stage") or "").strip()

    parts: list[str] = []
    if seniority:
        parts.append(seniority)
    if role:
        parts.append(role)
    base = " ".join(parts) or "engineer"

    if source == "linkedin":
        prefix = "LinkedIn profile of a"
    elif source == "github":
        prefix = "GitHub profile of a"
    else:  # x
        prefix = "X / Twitter profile of a"

    q = f"{prefix} {base}"
    if co_stage:
        q += f" working at a {co_stage}-stage startup"
    return q


# ---- per-source parsing --------------------------------------------------

def _parse_result(source: str, r: dict) -> Optional[dict]:
    url = (r.get("url") or "").strip()
    title = (r.get("title") or "").strip()
    text = (r.get("text") or "").strip()
    if not url:
        return None

    if source == "linkedin":
        m = _LINKEDIN_RE.search(url)
        if not m:
            return None
        handle = m.group(1)
        name, role, company = _parse_linkedin_title(title)
        if not name:
            return None
        # Filter out org/company pages that snuck through (the category
        # filter helps but isn't bulletproof — e.g., "UCD Sociology",
        # "Supreme Incubator" came back for a Senior+ engineer query).
        if _looks_like_org(name):
            return None
        # When title parsing didn't yield role/company, mine the page
        # snippet text. Exa returns ~500-1000 chars of page text with
        # `contents.text: true`; LinkedIn snippets typically include
        # the current role + company near the top.
        if not role or not company:
            r_from_text, c_from_text = _extract_role_company_from_text(text)
            role = role or r_from_text
            company = company or c_from_text
        return {
            "identity": handle,
            "name": name,
            "linkedin_url": _normalize_linkedin_url(url, handle),
            "role": role,
            "company": company,
            "contact_resolved": True,
            # Pass the snippet through so the downstream LLM judge has
            # extra context beyond the structured fields (which Exa
            # sometimes leaves empty).
            "description": text[:600],
        }

    if source == "github":
        m = _GITHUB_RE.search(url)
        if not m:
            return None
        handle = m.group(1)
        # Skip non-profile pages (orgs, /search, etc.)
        if handle.lower() in {"search", "topics", "explore", "marketplace",
                              "settings", "issues", "pulls", "notifications"}:
            return None
        name = _parse_github_title(title) or handle
        return {
            "identity": handle,
            "name": name,
            "github_url": url,
            "gh_stars": 0,
        }

    # x / twitter
    m = _X_RE.search(url)
    if not m:
        return None
    handle = m.group(1)
    if handle.lower() in {"home", "explore", "notifications", "messages",
                          "i", "settings", "search", "compose"}:
        return None
    name = _parse_x_title(title) or handle
    return {
        "identity": handle,
        "name": name,
        "x_url": url,
        "x_followers": 0,
    }


def _normalize_linkedin_url(url: str, handle: str) -> str:
    """Canonical form: https://www.linkedin.com/in/<handle>"""
    return f"https://www.linkedin.com/in/{handle}"


def _parse_linkedin_title(title: str) -> tuple[str, str, str]:
    """
    LinkedIn page titles come in many shapes in practice — sometimes:
      "Daniel Wang - Software Engineer at Acme | LinkedIn"
      "Daniel Wang - Software Engineer | LinkedIn"
      "Daniel Wang | LinkedIn"
      "Daniel Wang | Senior Engineer"
      "Daniel Wang | Senior Engineer | LinkedIn"
      "Daniel Wang"   (Exa often strips the trailer entirely)
    Returns (name, role, company); any field can be "".
    """
    if not title:
        return ("", "", "")

    # Strip a trailing " | LinkedIn" if present (case-insensitive)
    base = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.I).strip()
    # Some Exa results have "| LinkedIn" in the middle; strip that too
    base = re.sub(r"\s*\|\s*LinkedIn\s*\|\s*", " | ", base, flags=re.I).strip()

    # Try " - " as separator first (canonical pattern)
    if " - " in base:
        name, rest = base.split(" - ", 1)
        return _split_role_company(name.strip(), rest.strip())

    # Fall back to " | " as separator (Exa often uses this)
    # "Name | Role at Company" or "Name | Role" or "Name | Company"
    if " | " in base:
        name, rest = base.split(" | ", 1)
        return _split_role_company(name.strip(), rest.strip())

    # No separator — title is just the name (or unparseable garbage).
    # Heuristic: if it looks like a person name (≤4 words, no digits-heavy),
    # take it; otherwise treat as empty so we drop the result.
    if _looks_like_person_name(base):
        return (base, "", "")
    return ("", "", "")


def _split_role_company(name: str, rest: str) -> tuple[str, str, str]:
    """Given a name + remainder, figure out role + company from the rest."""
    if " at " in rest:
        role, company = rest.split(" at ", 1)
        # The company part can have another " | " separator: "Acme | LinkedIn"
        company = re.split(r"\s*\|\s*", company, maxsplit=1)[0]
        return (name, role.strip(), company.strip())
    return (name, rest, "")


# Heuristics ---------------------------------------------------------------

_DIGIT_RE = re.compile(r"\d")


def _looks_like_person_name(s: str) -> bool:
    """Cheap check: does this string read like a person's name?"""
    if not s:
        return False
    words = s.split()
    if len(words) < 1 or len(words) > 5:
        return False
    # Names rarely have digits
    if _DIGIT_RE.search(s):
        return False
    # First word should be a real-looking word (≥2 letters, mostly alpha)
    return len(words[0]) >= 2 and words[0][0].isalpha()


_ORG_HINTS = (
    "incubator", "sociology", "university", "school", "college",
    "department", "ventures", "capital", "fund", "investments",
    "labs", "studio", "agency", "consulting", "group", "associates",
    "council", "society", "association", "institute", "foundation",
    "academy", "team", "company", "corporation", "limited", "ltd",
    "inc", "llc", " co.", "events", "office",
)


def _looks_like_org(name: str) -> bool:
    """True when `name` looks like an organization, not a person."""
    if not name:
        return False
    lower = name.lower()
    return any(h in lower for h in _ORG_HINTS)


def _extract_role_company_from_text(text: str) -> tuple[str, str]:
    """
    Mine a LinkedIn page snippet for the current role + company.

    LinkedIn snippets typically open with something like:
      "Experience: Acme · Education: Stanford · Location: SF"
      "Senior ML Engineer at Acme | 500+ followers..."
      "Software engineer at Acme. Building..."

    We try a few common patterns. Returns ("", "") if nothing matches.
    """
    if not text:
        return ("", "")
    # Compact whitespace for matching
    flat = re.sub(r"\s+", " ", text).strip()
    # Pattern: "<Role> at <Company>" — only take the FIRST occurrence.
    # Role: 2-6 words, mostly letters. Company: 1-5 words.
    m = re.search(
        r"([A-Z][A-Za-z+/\- ]{2,80}?)\s+at\s+([A-Z][A-Za-z0-9&+./'\- ]{1,60}?)"
        r"(?:[\.\|\(,]|$)",
        flat,
    )
    if m:
        role = m.group(1).strip(" -·")
        company = m.group(2).strip(" -·")
        # Sanity: role shouldn't be a full sentence
        if len(role.split()) <= 8 and len(company.split()) <= 6:
            return (role, company)
    return ("", "")


def _parse_github_title(title: str) -> str:
    """
    GitHub pages typically look like one of:
      "username (Real Name) · GitHub"
      "username · GitHub"
    """
    if not title:
        return ""
    base = re.sub(r"\s*·\s*GitHub\s*$", "", title, flags=re.I).strip()
    m = re.match(r"^[A-Za-z0-9_-]+\s*\(([^)]+)\)\s*$", base)
    if m:
        return m.group(1).strip()
    return ""


def _parse_x_title(title: str) -> str:
    """
    X pages typically look like:
      "Real Name (@handle) / X"
      "Real Name (@handle) on X: ..."
    """
    if not title:
        return ""
    m = re.match(r"^([^(]+?)\s*\(@[A-Za-z0-9_]+\)", title)
    if m:
        return m.group(1).strip()
    return ""
