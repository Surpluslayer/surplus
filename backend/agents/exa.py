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
        # parseable handle (snippets, archives, etc.). Exa caps at 100 per
        # request so clamp there even when max_candidates is high.
        "numResults": min(100, max(max_candidates * 3, 10)),
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
    """
    Compose a semantic query Exa can match. Reads like a description, not
    a database query — Exa's neural search responds best to natural
    phrasing without articles. "Senior ML engineers at seed startups"
    pulls way more profiles than "LinkedIn profile of a Senior ML
    engineer working at a Seed-stage startup" (awkward "a + plural").
    """
    role = (icp.get("role") or "").strip()
    seniority = (icp.get("seniority") or "").strip().rstrip("+")
    co_stage = (icp.get("co_stage") or "").strip()

    # Seniority adjective: "Senior", "Staff", "Principal", "Mid", "Leadership"
    # → folds Staff+ to "Staff" for natural reading.
    seniority_word = ""
    if seniority:
        sl = seniority.lower()
        if "leadership" in sl:
            seniority_word = "senior leadership"
        else:
            seniority_word = seniority.lower()

    # Pluralize role for natural matching. Just append 's' if needed.
    role_phrase = role.lower() if role else "engineer"
    if role_phrase and not role_phrase.endswith("s"):
        role_phrase += "s"

    base = f"{seniority_word} {role_phrase}".strip()

    # Anchor by stage when present. Drop the article — "seed startups"
    # reads naturally; "a Seed-stage startup" introduces grammar friction
    # that hurts neural matching.
    if co_stage:
        stage_phrase = co_stage.lower().replace("-stage", "").strip()
        base = f"{base} at {stage_phrase} startups"

    # Source-specific prefix that anchors the platform without forcing
    # singular grammar.
    prefix = {
        "linkedin": "linkedin profile",
        "github":   "github profile",
        "x":        "x / twitter profile",
    }[source]

    return f"{prefix} {base}".strip()


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
        # the current role + company near the top in a structured form.
        if not role or not company:
            r_from_text, c_from_text = _extract_role_company_from_text(text)
            role = role or r_from_text
            company = company or c_from_text
        headline = _extract_headline_from_text(text)
        return {
            "identity": handle,
            "name": name,
            "linkedin_url": _normalize_linkedin_url(url, handle),
            "role": role,
            "company": company,
            "contact_resolved": True,
            # Inferred from role + headline text. The scorer uses this to
            # decide the seniority bonus vs the event's target — without it
            # everyone defaults to 'Mid' and gets a -8 penalty against any
            # Senior+ ICP, which is exactly what was wrong before.
            "seniority": _infer_seniority(role, headline),
            # Headline is the one-liner bio under the name. The LLM judge
            # reads this as a high-signal summary of who the person is.
            "headline": headline,
            # Full snippet for additional context (truncated).
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


_HEADER_RE = re.compile(r"^#+\s+")
_AT_LINE_RE = re.compile(
    # Strip leading markdown header (## / ### / ####)
    r"^(?:#+\s+)?"
    # "<Role> at [<Company>](url)" — markdown link form
    # "<Role> at <Company>"       — plain form, greedy (terminates at newline)
    r"(?P<role>.+?)\s+at\s+"
    r"(?:\[(?P<company_link>[^\]]+)\]\([^)]*\)|(?P<company_plain>[^|()\n]+))",
    re.IGNORECASE,
)
_SECTION_KEYWORDS = ("about", "experience", "education", "skills",
                     "licenses", "certifications", "languages")

# Role-keyword → seniority bucket. The scorer (backend/agents/scorer.py)
# expects one of: Mid / Senior / Staff+ / Leadership. Exa's structured
# fields don't carry seniority, so we infer from the role + headline text.
# Order matters — first match wins, most senior bucket first.
_SENIORITY_HINTS: tuple[tuple[str, str], ...] = (
    ("Leadership", "founder"),
    ("Leadership", "ceo"),
    ("Leadership", "cto"),
    ("Leadership", "cpo"),
    ("Leadership", "coo"),
    ("Leadership", "vp "),
    ("Leadership", "vp,"),
    ("Leadership", "vice president"),
    ("Leadership", "head of"),
    ("Leadership", "chief "),
    ("Leadership", "director"),
    ("Leadership", "partner"),
    ("Staff+", "principal "),
    ("Staff+", "staff "),
    ("Staff+", "distinguished "),
    ("Staff+", "fellow "),
    ("Staff+", "founding "),  # "founding engineer" = Staff+ at a seed startup
    ("Staff+", " lead"),
    ("Senior", "senior "),
    ("Senior", "sr. "),
    ("Senior", "sr "),
    ("Mid", "junior "),
    ("Mid", "associate "),
    ("Mid", "entry "),
)


def _infer_seniority(*texts: str) -> str:
    """Pick a seniority bucket from one or more role/headline strings.

    Defaults to 'Senior' when no keyword matches — most LinkedIn-discovered
    professionals are at least Senior, and 'Mid' would mean a -8 hit
    against any Senior+ ICP target which we don't want by default.
    """
    haystack = " ".join(t.lower() for t in texts if t)
    if not haystack:
        return "Senior"
    for bucket, needle in _SENIORITY_HINTS:
        if needle in haystack:
            return bucket
    return "Senior"
_DATE_TRAILER_RE = re.compile(
    r"\s+(?:\(?Current\)?|\d{4}\s*[-–]\s*(?:Present|\d{4}).*|\d{4}\s*[-–]\s*\d{4}.*)$",
    re.IGNORECASE,
)


def _extract_role_company_from_text(text: str) -> tuple[str, str]:
    """
    Mine a LinkedIn page snippet for the CURRENT role + company.

    Exa returns LinkedIn snippets as markdown:

        # <Name>
        <Headline>                       ← bio with possible "@" but rarely "at"
        <Current Role> at [<Company>](url)   ← canonical "current" line
        <Location>                       ← skip
        ## Experience
        ### <Role> at [<Company>](url)   ← first one is the current job

    We walk every line (no early break on `## Section`), skipping noise
    lines (location, follower counts, pure section headers), and return
    the first "Role at Company" match. The headline is also skipped
    because it's typically pipe-separated bio text — `_extract_headline_
    from_text` captures it separately.
    """
    if not text:
        return ("", "")

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # Drop the leading "# Name" header
    if lines and _HEADER_RE.match(lines[0]) and not lines[0].startswith("##"):
        lines = lines[1:]
    # Skip the headline line — usually pipe-separated buzzwords or has
    # "@" but not " at ". Don't skip if it already matches our pattern.
    if lines and " at " not in lines[0].lower() and (
        "|" in lines[0] or "@" in lines[0]
    ):
        lines = lines[1:]

    for line in lines:
        # Skip location lines ("Berkeley, California, United States (US)")
        if re.search(r"\([A-Z]{2}\)\s*$", line):
            continue
        # Skip count / metadata lines
        lower = line.lower()
        if "connection" in lower or "follower" in lower:
            continue
        # Skip pure section names (## About, ### Skills, etc.)
        stripped = line.lower().strip("# :")
        if stripped in _SECTION_KEYWORDS:
            continue
        # Skip total-experience summaries
        if lower.startswith("total experience"):
            continue
        # Skip lines that are too long to be a role — likely descriptive
        if len(line) > 200:
            continue

        m = _AT_LINE_RE.match(line)
        if not m:
            continue
        role = (m.group("role") or "").strip(" -·#")
        company = (m.group("company_link") or m.group("company_plain") or "").strip(" -·")
        # Strip trailing date/range/"(Current)" from the company side
        company = _DATE_TRAILER_RE.sub("", company).strip()
        # Sanity caps
        if not (1 <= len(role.split()) <= 12):
            continue
        if not (1 <= len(company.split()) <= 8):
            continue
        if len(role) < 2 or len(company) < 1:
            continue
        return (role, company)
    return ("", "")


def _extract_headline_from_text(text: str) -> str:
    """
    Pull the LinkedIn headline (the one-line bio under the name).

    Goes between the "# Name" header and the structured Role-at-Company
    block. Often the single richest signal about who the person is.
    """
    if not text:
        return ""
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if _HEADER_RE.match(s):
            continue
        if s.startswith("##"):
            break
        # Skip Role-at-Company structured lines — we capture those elsewhere
        if _AT_LINE_RE.match(s) and "|" not in s:
            continue
        return s[:200]
    return ""


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
