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
    body = {
        "query": query,
        "type": "neural",
        # over-fetch — many results won't have a matching profile URL
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
        return {
            "identity": handle,
            "name": name,
            "linkedin_url": _normalize_linkedin_url(url, handle),
            "role": role,
            "company": company,
            "contact_resolved": True,
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
    LinkedIn pages typically look like one of:
      "Daniel Wang - Software Engineer at Acme | LinkedIn"
      "Daniel Wang - Software Engineer | LinkedIn"
      "Daniel Wang | LinkedIn"
    Returns (name, role, company); any field can be "".
    """
    if not title:
        return ("", "", "")
    # strip "| LinkedIn" trailer
    base = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.I).strip()
    # split on first " - "
    if " - " in base:
        name, rest = base.split(" - ", 1)
        if " at " in rest:
            role, company = rest.split(" at ", 1)
            return (name.strip(), role.strip(), company.strip())
        return (name.strip(), rest.strip(), "")
    return (base.strip(), "", "")


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
