"""
agents/exa.py : Exa-backed prospect discovery.

Same contract as `llm.discover_candidates(source, icp)` : returns a list of
candidate dicts in the per-source shape : but uses Exa's semantic search
instead of Claude + web_search. Cheaper, faster, and Exa's index has good
LinkedIn / GitHub / X coverage so we can extract the canonical profile URL
straight from the result without an extra parsing step.

Gated by EXA_API_KEY. When unset, callers fall back to llm.discover_candidates
(Claude) and ultimately the mock pool.

Result shapes per source : matching what the existing SourceAdapter expects:

  linkedin: {identity, name, linkedin_url, role?, company?, contact_resolved: True}
  github  : {identity, name, github_url, gh_stars: 0}
  x       : {identity, name, x_url, x_followers: 0}

The 0s for gh_stars / x_followers are because Exa's index returns metadata
about the page, not live API data. The scorer accepts 0 gracefully : the
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
# Scholar surfaces from three places. Google Scholar carries a stable
# author id in `?user=<id>`; Semantic Scholar uses /author/<slug>/<id>;
# arXiv author pages use /a/<id>. First capture group is the handle we
# slugify into `identity`.
_SCHOLAR_GOOGLE_RE = re.compile(r"scholar\.google\.com/citations\?[^\s]*user=([A-Za-z0-9_-]+)", re.I)
_SCHOLAR_SEMANTIC_RE = re.compile(r"semanticscholar\.org/author/[^/]+/(\d+)", re.I)
_SCHOLAR_ARXIV_RE = re.compile(r"arxiv\.org/a/([A-Za-z0-9_-]+)", re.I)
# "Cited by 1,234" / "1234 citations" on Scholar / Semantic Scholar snippets.
_CITED_BY_RE = re.compile(r"cited\s+by\s+([\d,]+)", re.I)
_CITATIONS_RE = re.compile(r"([\d,]+)\s+citations?\b", re.I)

# Title parsing : LinkedIn page titles follow a consistent format
_LI_TITLE_RE = re.compile(r"^(.+?)\s*-\s*(.+?)\s*(?:\|\s*LinkedIn)?\s*$")


# ---- city normalization --------------------------------------------------
#
# LinkedIn locations are written many different ways for the same place
# ("San Francisco", "San Francisco Bay Area", "SF", "Bay Area", "Oakland").
# Three things rely on this:
#
#   (1) Query phrasing  : we want "in the san francisco bay area" because
#       that's literally what LinkedIn profile pages say.
#   (2) includeText     : Exa's server-side hard filter (1 phrase, ≤5 words).
#       Pick the *shortest substring that appears in every alias* so we
#       don't over-prune. "San Francisco" matches "San Francisco" AND
#       "San Francisco Bay Area"; "San Francisco Bay Area" would miss the
#       former.
#   (3) Post-filter     : broader set of aliases scanned against the
#       returned snippet text so we drop NYC profiles that snuck through
#       ranking even after includeText.
#
# Entries are keyed by the lowercase form of what the user typed on intake.
# `canonical_phrase` goes into the query, `include_text` into the Exa filter,
# `aliases` into the post-filter scan.

_CITY_ALIASES: dict[str, dict] = {
    "sf": {
        "canonical_phrase": "the san francisco bay area",
        "include_text": "San Francisco",
        "aliases": ("san francisco", "bay area", "oakland", "berkeley",
                    "palo alto", "mountain view", "menlo park", "sf"),
    },
    "san francisco": {
        "canonical_phrase": "the san francisco bay area",
        "include_text": "San Francisco",
        "aliases": ("san francisco", "bay area", "oakland", "berkeley",
                    "palo alto", "mountain view", "menlo park", "sf"),
    },
    "bay area": {
        "canonical_phrase": "the san francisco bay area",
        "include_text": "San Francisco",
        "aliases": ("san francisco", "bay area", "oakland", "berkeley",
                    "palo alto", "mountain view", "menlo park", "sf"),
    },
    "nyc": {
        "canonical_phrase": "new york city",
        "include_text": "New York",
        "aliases": ("new york", "nyc", "brooklyn", "manhattan", "queens"),
    },
    "new york": {
        "canonical_phrase": "new york city",
        "include_text": "New York",
        "aliases": ("new york", "nyc", "brooklyn", "manhattan", "queens"),
    },
    "new york city": {
        "canonical_phrase": "new york city",
        "include_text": "New York",
        "aliases": ("new york", "nyc", "brooklyn", "manhattan", "queens"),
    },
    "la": {
        "canonical_phrase": "the los angeles area",
        "include_text": "Los Angeles",
        "aliases": ("los angeles", "la", "santa monica", "pasadena", "venice"),
    },
    "los angeles": {
        "canonical_phrase": "the los angeles area",
        "include_text": "Los Angeles",
        "aliases": ("los angeles", "la", "santa monica", "pasadena", "venice"),
    },
    "seattle": {
        "canonical_phrase": "the seattle area",
        "include_text": "Seattle",
        "aliases": ("seattle", "bellevue", "redmond"),
    },
    "austin": {
        "canonical_phrase": "austin, texas",
        "include_text": "Austin",
        "aliases": ("austin", "texas"),
    },
    "boston": {
        "canonical_phrase": "the boston area",
        "include_text": "Boston",
        "aliases": ("boston", "cambridge", "somerville"),
    },
    "london": {
        "canonical_phrase": "london, united kingdom",
        "include_text": "London",
        "aliases": ("london", "united kingdom"),
    },
}


def _resolve_city(raw: str) -> Optional[dict]:
    """Return city config for a raw user-typed city, or None for an unknown
    city. Unknown cities still work : caller falls back to using the raw
    string as both the query phrase and the includeText (best-effort).
    """
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]
    # Unknown city : synthesize a config so the rest of the pipeline still
    # works. include_text is the user's literal input; aliases include just
    # the input itself.
    return {
        "canonical_phrase": key,
        "include_text": raw.strip(),
        "aliases": (key,),
    }


# A "location-like" line in a LinkedIn snippet : used by the post-filter to
# decide whether a snippet HAS location information at all. If a profile
# snippet has zero location-looking lines we keep it (don't over-prune);
# if it has one and our city's aliases don't appear anywhere in the snippet,
# we drop it.
_LOCATION_HINT_RE = re.compile(
    r"\([A-Z]{2}\)\s*$|"                       # "...United States (US)"
    r",\s*(california|new york|texas|"         # state names
    r"washington|massachusetts|illinois|"
    r"colorado|georgia|florida)\b|"
    r"\b(bay area|metropolitan area)\b",       # "Greater Boston Area"
    re.IGNORECASE,
)


def _location_matches(snippet: str, aliases: tuple[str, ...]) -> bool:
    """True if the snippet either:
      - mentions any of the city aliases anywhere, OR
      - contains no location-looking line at all (can't disprove : keep).
    False only when there's a location line AND none of the aliases match.
    """
    if not snippet:
        return True  # nothing to check; keep
    lower = snippet.lower()
    for alias in aliases:
        if alias in lower:
            return True
    # No alias matched : but if the snippet has no location signal we can't
    # confidently reject. Only drop when there's an actual location line.
    has_location_line = any(
        _LOCATION_HINT_RE.search(line) for line in snippet.split("\n")
    )
    return not has_location_line


def discover_via_exa(source: str, icp: dict, max_candidates: int = 5) -> list[dict]:
    """
    Search Exa for one source's candidates matching the ICP.

    Uses Exa's `category` filter to scope to actual profile pages : this is
    more precise than `includeDomains` alone (which would also surface
    LinkedIn job posts, company pages, etc.). We pass both as belt-and-
    suspenders so we don't pay tokens reading pages we'll discard anyway.

    Returns up to `max_candidates` dicts. On any error, returns [] so the
    caller can fall through to another backend or the mock pool.
    """
    if not exa_available():
        return []
    if source not in ("linkedin", "github", "x", "scholar"):
        return []

    city_cfg = _resolve_city(icp.get("city") or "")
    query = _build_query(source, icp, city_cfg)
    # Scholar covers three index sources : Google Scholar (primary, has the
    # citation count), Semantic Scholar (richer metadata, broader coverage),
    # and arXiv (preprints, useful for ML researchers). We pass all three so
    # one Exa search reaches everywhere. No `category` filter because Exa's
    # "research paper" category surfaces PAPER pages (PDFs, /abs/<id>,
    # /paper/<id>) which our parser discards; we want AUTHOR profile pages
    # (?user=<id>, /author/<slug>/<id>, /a/<id>) so we let neural matching
    # against the query phrasing do the scoping.
    if source == "scholar":
        domain = ["scholar.google.com", "semanticscholar.org", "arxiv.org"]
        category = None
    else:
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
        # over-fetch : even with category filter, some results won't yield a
        # parseable handle (snippets, archives, etc.). Exa caps at 100 per
        # request so clamp there even when max_candidates is high. Bump the
        # multiplier when city is set so the post-filter has headroom to
        # drop wrong-city results without starving max_candidates.
        "numResults": min(100, max(max_candidates * (5 if city_cfg else 3), 10)),
        "includeDomains": domain if isinstance(domain, list) else [domain],
        "contents": {"text": True},
    }
    if category:
        body["category"] = category
    # Exa server-side hard filter : only return pages whose text contains
    # this phrase. Massively cuts wrong-geo results before they hit our
    # parser. Only do this for LinkedIn since github/x profile pages rarely
    # carry a clean location string Exa can match.
    if city_cfg and source == "linkedin":
        body["includeText"] = [city_cfg["include_text"]]
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
        cand = _parse_result(source, r, city_cfg)
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

def _as_list(v) -> list[str]:
    """Accept a list (frontend shape), a CSV string (storage shape), or empty.
    Always returns a list of trimmed non-empty strings.
    """
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if not v:
        return []
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _seniority_word(s: str) -> list[str]:
    """One selected seniority chip -> the word(s) Exa should search for.
    `Staff+` is expanded so the query catches Staff/Principal/Distinguished
    titles, not just literal "staff".
    """
    sl = s.strip().rstrip("+").lower()
    if not sl:
        return []
    if "leadership" in sl:
        return ["senior leadership"]
    if sl == "staff":
        return ["staff", "principal", "distinguished"]
    if sl == "student":
        # On LinkedIn / Scholar these surface variously as "student",
        # "PhD student", or "graduate student" : casting the wider net
        # catches all three.
        return ["student", "phd student", "graduate student"]
    return [sl]


def _build_query(source: str, icp: dict, city_cfg: Optional[dict] = None) -> str:
    """
    Compose a semantic query Exa can match. Reads like a description, not
    a database query : Exa's neural search responds best to natural
    phrasing without articles. "Senior ML engineers at seed startups"
    pulls way more profiles than "LinkedIn profile of a Senior ML
    engineer working at a Seed-stage startup" (awkward "a + plural").

    Seniority and co_stage are multi-select : emitted as OR-clauses
    ("senior or staff or principal engineers at seed or series a startups")
    so one Exa request covers all selected chips.
    """
    role = (icp.get("role") or "").strip()
    seniorities = _as_list(icp.get("seniority"))
    co_stages = _as_list(icp.get("co_stage"))
    # Use the resolved canonical city phrase when available : "the san
    # francisco bay area" matches LinkedIn's literal location strings much
    # better than raw user input like "sf". Fall back to raw text for
    # unknown cities (and tests that pass icp without going through
    # discover_via_exa).
    if city_cfg:
        city = city_cfg["canonical_phrase"]
    else:
        city = (icp.get("city") or "").strip()

    # Dedupe while preserving order (Staff+ + Senior shouldn't double "senior")
    sen_words: list[str] = []
    for s in seniorities:
        for w in _seniority_word(s):
            if w not in sen_words:
                sen_words.append(w)
    seniority_word = " or ".join(sen_words)

    # Pluralize role for natural matching. Just append 's' if needed.
    role_phrase = role.lower() if role else "engineer"
    if role_phrase and not role_phrase.endswith("s"):
        role_phrase += "s"

    base = f"{seniority_word} {role_phrase}".strip()

    # Anchor by stage when present. Drop the article : "seed startups"
    # reads naturally; "a Seed-stage startup" introduces grammar friction
    # that hurts neural matching. "Enterprise" sounds wrong as "enterprise
    # startups", so route it to "enterprise companies".
    if co_stages:
        startup_phrases: list[str] = []
        enterprise = False
        for st in co_stages:
            p = st.lower().replace("-stage", "").strip()
            if not p:
                continue
            if p == "enterprise":
                enterprise = True
            elif p not in startup_phrases:
                startup_phrases.append(p)
        clauses: list[str] = []
        if startup_phrases:
            clauses.append(f"{' or '.join(startup_phrases)} startups")
        if enterprise:
            clauses.append("enterprise companies")
        if clauses:
            base = f"{base} at {' or '.join(clauses)}"

    # Anchor by years of experience when present : Exa surfaces YOE in
    # LinkedIn snippets as "8 years experience" / "10+ years" so the natural
    # phrasing matches the actual page text.
    yoe_buckets = _as_list(icp.get("yoe"))
    if yoe_buckets:
        base = f"{base} with {' or '.join(yoe_buckets)} years experience"

    # Anchor by city when present. Exa's neural index matches against the
    # profile page text, where LinkedIn typically surfaces the location
    # near the headline ("San Francisco Bay Area"). Without this, every
    # search returned the global pool and the city field on intake had no
    # effect on who got surfaced.
    if city:
        base = f"{base} in {city.lower()}"

    # Source-specific prefix that anchors the platform without forcing
    # singular grammar. Scholar drops the role-based phrasing in favor of
    # research-domain phrasing ("researcher" reads more naturally than
    # "engineer" on Scholar profile pages and matches the snippet text).
    prefix = {
        "linkedin": "linkedin profile",
        "github":   "github profile",
        "x":        "x / twitter profile",
        "scholar":  "google scholar researcher",
    }[source]

    return f"{prefix} {base}".strip()


# ---- per-source parsing --------------------------------------------------

def _parse_result(source: str, r: dict, city_cfg: Optional[dict] = None) -> Optional[dict]:
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
        # filter helps but isn't bulletproof : e.g., "UCD Sociology",
        # "Supreme Incubator" came back for a Senior+ engineer query).
        if _looks_like_org(name):
            return None
        # Belt-and-suspenders geo filter. includeText already drops most
        # wrong-city results at Exa, but Exa's text-match is fuzzy enough
        # that "San Francisco, the band" or stale education entries can
        # slip through. Re-scan the snippet for our city's aliases and
        # drop the result if there's a location line that doesn't match.
        if city_cfg and not _location_matches(text, city_cfg["aliases"]):
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
            # decide the seniority bonus vs the event's target : without it
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

    if source == "scholar":
        return _parse_scholar_result(url, title, text)

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


def _parse_scholar_result(url: str, title: str, text: str) -> Optional[dict]:
    """Pick out the author identity, name, and citation count from a
    Google Scholar / Semantic Scholar / arXiv search result.

    We don't attempt to verify ICP fit here : the prospect merge handles
    that by only attaching scholar_citations to identities that *also*
    surfaced from another adapter (LinkedIn / GitHub / X). A pure-Scholar
    candidate without a LinkedIn URL gets dropped by prospector.py.
    """
    handle = ""
    m = _SCHOLAR_GOOGLE_RE.search(url)
    if m:
        handle = m.group(1)
    if not handle:
        m = _SCHOLAR_SEMANTIC_RE.search(url)
        if m:
            handle = f"ss-{m.group(1)}"
    if not handle:
        m = _SCHOLAR_ARXIV_RE.search(url)
        if m:
            handle = f"arxiv-{m.group(1)}"
    if not handle:
        return None

    name = _parse_scholar_title(title)
    if not name:
        return None
    if _looks_like_org(name):
        return None
    # Prefer a name-slug as `identity` so the merge can attach this signal
    # to the LinkedIn/GitHub-anchored record for the same person. The
    # platform handle is preserved on `scholar_url` for traceability.
    identity = _name_slug(name) or handle

    citations = _extract_citations(text) or _extract_citations(title)
    return {
        "identity": identity,
        "name": name,
        "scholar_url": url,
        "scholar_citations": citations,
    }


def _name_slug(name: str) -> str:
    """Lowercase-hyphenated name slug. Same convention the LLM is asked to
    emit so cross-source merging matches across backends."""
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned


def _parse_scholar_title(title: str) -> str:
    """Pull a person's name out of a Scholar/SemanticScholar/arXiv title.

    Common shapes:
      "Maya Rodriguez - Google Scholar"
      "‪Maya Rodriguez‬ - ‪Google Scholar‬"
      "Maya Rodriguez | Semantic Scholar"
      "Maya Rodriguez - arXiv.org"
    """
    if not title:
        return ""
    # Strip Unicode directional marks Scholar wraps names in
    base = title.replace("‪", "").replace("‬", "").strip()
    base = re.sub(r"\s*[-–|·]\s*(google scholar|semantic scholar|arxiv\.org|arxiv).*$",
                  "", base, flags=re.I).strip()
    if _looks_like_person_name(base):
        return base
    return ""


def _extract_citations(text: str) -> int:
    """Pull the highest plausible citation count from a Scholar snippet.

    Snippets often carry several numbers : "Cited by 1,234 · h-index 12".
    We take the first match of "Cited by" / "X citations"; if neither
    appears, returns 0 (the candidate may still be useful if attached to
    a stronger source).
    """
    if not text:
        return 0
    m = _CITED_BY_RE.search(text) or _CITATIONS_RE.search(text)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def _normalize_linkedin_url(url: str, handle: str) -> str:
    """Canonical form: https://www.linkedin.com/in/<handle>"""
    return f"https://www.linkedin.com/in/{handle}"


def _parse_linkedin_title(title: str) -> tuple[str, str, str]:
    """
    LinkedIn page titles come in many shapes in practice : sometimes:
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

    # No separator : title is just the name (or unparseable garbage).
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
    # "<Role> at [<Company>](url)" : markdown link form
    # "<Role> at <Company>"       : plain form, greedy (terminates at newline)
    r"(?P<role>.+?)\s+at\s+"
    r"(?:\[(?P<company_link>[^\]]+)\]\([^)]*\)|(?P<company_plain>[^|()\n]+))",
    re.IGNORECASE,
)
_SECTION_KEYWORDS = ("about", "experience", "education", "skills",
                     "licenses", "certifications", "languages")

# Role-keyword → seniority bucket. The scorer (backend/agents/scorer.py)
# expects one of: Student / Mid / Senior / Staff+ / Leadership. Exa's
# structured fields don't carry seniority, so we infer from the role +
# headline text. Order matters : first match wins, most senior bucket first.
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
    # Student bucket : interns, PhDs, anyone still in school. The keyword
    # is broad ("student" alone) but the more-senior matches above run
    # first so a "Staff Engineer · part-time student" still resolves to
    # Staff+, which is what we want.
    ("Student", "phd student"),
    ("Student", "graduate student"),
    ("Student", "intern"),
    ("Student", "student"),
)


def _infer_seniority(*texts: str) -> str:
    """Pick a seniority bucket from one or more role/headline strings.

    Defaults to 'Senior' when no keyword matches : most LinkedIn-discovered
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
    because it's typically pipe-separated bio text : `_extract_headline_
    from_text` captures it separately.
    """
    if not text:
        return ("", "")

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # Drop the leading "# Name" header
    if lines and _HEADER_RE.match(lines[0]) and not lines[0].startswith("##"):
        lines = lines[1:]
    # Skip the headline line : usually pipe-separated buzzwords or has
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
        # Skip lines that are too long to be a role : likely descriptive
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
        # Skip Role-at-Company structured lines : we capture those elsewhere
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
