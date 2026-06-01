"""
triage/enrich.py : dual-path evidence COLLECTION via Unipile (LinkedIn) + Exa.

This module fetches raw evidence and returns *candidates*, not a decision. It
deliberately does NOT pick "the" company or merge sources — that judgment moved
to reconcile.py, which can weigh contradictions, and ultimately to the scorer,
which sees the rejected candidates too.

Why : a single bad company-search hit used to silently poison the verdict. The
classic failure was two distinct companies sharing a name ("Kyndred") where the
code picked the one matching the event theme over the one matching the person.
Collecting all candidates + their provenance lets later stages reason instead of
trusting one collapsed snippet.

What it collects, per applicant:

  Person evidence (Unipile LinkedIn profile):
    headline, location, about, work experience, current company, name-match.

  Company candidates (one per source hit, NOT deduped to a winner):
    - every LinkedIn company-search result whose name matches the claim
    - the Exa co-occurrence hit for "{person} {company} founder"
    - the applicant's submitted website (if any)
  Each candidate carries structural match flags (name / headline / work-exp /
  domain / co-occurrence) and warnings (low followers, no website, etc.).

  Raw searches: the unprocessed Unipile + Exa payloads, for the debug artifact.

LinkedIn account pool:
  UNIPILE_TRIAGE_ACCOUNT_IDS (comma-separated, round-robin); falls back to
  UNIPILE_ACCOUNT_ID. Both pools rotate so profile lookups spread across
  available accounts.

Failures are silent — enrichment is supplementary signal, never load-bearing.
"""
from __future__ import annotations
import itertools
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ..agents import exa as exa_agent
from . import unipile_adapter
from .unipile_adapter import COUNTERS, ProfileResult


# ── Unipile helpers ───────────────────────────────────────────────────────

def _unipile_dsn() -> str:
    return (os.environ.get("UNIPILE_DSN") or "").strip().rstrip("/")

def _unipile_api_key() -> str:
    return (os.environ.get("UNIPILE_TRIAGE_API_KEY")
            or os.environ.get("UNIPILE_API_KEY") or "").strip()

def _unipile_account_ids() -> list[str]:
    """Triage pool + main account, deduplicated."""
    seen: set[str] = set()
    ids: list[str] = []
    for raw in [
        os.environ.get("UNIPILE_TRIAGE_ACCOUNT_IDS") or "",
        os.environ.get("UNIPILE_ACCOUNT_ID") or "",
    ]:
        for a in raw.split(","):
            a = a.strip()
            if a and a not in seen:
                seen.add(a)
                ids.append(a)
    return ids

_POOL_LOCK = threading.Lock()
_POOL_CYCLE: itertools.cycle | None = None

def _next_account_id() -> str | None:
    global _POOL_CYCLE
    ids = _unipile_account_ids()
    if not ids:
        return None
    with _POOL_LOCK:
        if _POOL_CYCLE is None:
            _POOL_CYCLE = itertools.cycle(ids)
        return next(_POOL_CYCLE)


def _people_search_account_id() -> str | None:
    """Account used for LinkedIn people search. Pinned via
    UNIPILE_PEOPLE_SEARCH_ACCOUNT_ID (set to the Daniel Wang account, which holds
    the search subscription); falls back to the round-robin pool if unset."""
    pinned = (os.environ.get("UNIPILE_PEOPLE_SEARCH_ACCOUNT_ID") or "").strip()
    return pinned or _next_account_id()


# ── Per-account rate budget ────────────────────────────────────────────────
# The single biggest cause of stripped-200s (empty work-experience on a 200) is
# hammering ONE LinkedIn account too fast: under sustained load LinkedIn quietly
# drops the expensive experience section. The fix is to PACE each account — never
# fire two profile fetches on the same account_id within UNIPILE_MIN_FETCH_INTERVAL
# seconds. This is keyed BY ACCOUNT, so adding more accounts to the pool linearly
# raises throughput (each gets its own budget) instead of just throttling N
# accounts at once. Thread-safe because fetches run under asyncio.to_thread.
_RATE_LOCK = threading.Lock()
_ACCOUNT_NEXT_OK: dict[str, float] = {}   # account_id -> earliest monotonic ts


def _account_min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("UNIPILE_MIN_FETCH_INTERVAL", "3.0")))
    except (TypeError, ValueError):
        return 3.0


def _pace_account(account_id: str | None) -> None:
    """Block until this account is allowed to fetch again, then reserve its next
    slot. Reserving INSIDE the lock (before sleeping outside it) means concurrent
    threads queue deterministically rather than all racing the same instant."""
    if not account_id:
        return
    interval = _account_min_interval()
    if interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        earliest = _ACCOUNT_NEXT_OK.get(account_id, 0.0)
        start = max(now, earliest)
        _ACCOUNT_NEXT_OK[account_id] = start + interval
    wait = start - time.monotonic()
    if wait > 0:
        time.sleep(wait)


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


# ── Name helpers ──────────────────────────────────────────────────────────

_COMPANY_NAME_STOP: frozenset[str] = frozenset({
    "inc", "llc", "ltd", "corp", "co", "ai", "app", "labs", "io",
    "the", "and", "for", "of", "by", "at", "a", "an", "my", "your",
})


def _name_tokens(company_name: str) -> frozenset[str]:
    tokens = re.sub(r"[^a-z0-9 ]", " ", (company_name or "").lower()).split()
    return frozenset(t for t in tokens if len(t) >= 2 and t not in _COMPANY_NAME_STOP)


def _linkedin_name_match(query_name: str, result_name: str) -> bool:
    tokens = _name_tokens(query_name)
    if not tokens:
        return True
    return any(t in (result_name or "").lower() for t in tokens)


def _domain(url: str) -> str:
    try:
        return urlparse(url if "://" in (url or "") else f"//{url}").netloc.lower().replace("www.", "")
    except ValueError:
        return ""


def _person_name_parts(person_name: str) -> list[str]:
    return [p.lower() for p in (person_name or "").split() if len(p) > 2]


def _name_word_tokens(*names: str) -> list[str]:
    """All alphabetic name tokens (incl. short ones), lowercased, punctuation
    stripped. Unlike _person_name_parts this keeps 2-char tokens and initials
    so abbreviated profiles ('Jeff L.') can still be matched."""
    out: list[str] = []
    for nm in names:
        for raw in (nm or "").replace(".", " ").split():
            t = "".join(ch for ch in raw.lower() if ch.isalpha())
            if t:
                out.append(t)
    return out


def _name_matches(applicant_name: str, *profile_names: str) -> bool:
    """Tolerant person-name match between the submitted name and the LinkedIn
    profile name. Handles nickname/abbreviation forms the old substring check
    missed (e.g. 'Jeffrey Li' vs profile first='Jeff' last='L.').

    A match requires at least one STRONG token correspondence: two tokens where
    one is a prefix of the other and the shorter is ≥3 chars (jeffrey↔jeff), or
    exact equality (li↔li). One strong match on a distinctive name token is
    enough. We deliberately do NOT match on bare initials — 'J. Smith' vs
    'J. Sanders' share initials but aren't the same person, so an initials-only
    profile stays unmatched (→ identity_confidence low → human review)."""
    a = _name_word_tokens(applicant_name)
    p = _name_word_tokens(*profile_names)
    if not a or not p:
        return True  # can't disprove with no tokens on one side

    def _strong(x: str, y: str) -> bool:
        if x == y:
            return True
        short, lng = (x, y) if len(x) <= len(y) else (y, x)
        return len(short) >= 3 and lng.startswith(short)

    return any(_strong(x, y) for x in a for y in p)


# Free / generic email providers — an address here tells us nothing about the
# applicant's company, so we never use it as a company signal.
_FREEMAIL: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "mail.com",
    "hey.com", "fastmail.com", "zoho.com", "qq.com", "163.com", "126.com",
    "duck.com", "yandex.com", "hotmail.co.uk", "yahoo.co.uk",
})


def _email_domain(email: str) -> str:
    """Company domain from an email, or '' for free/generic providers."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    dom = e.rsplit("@", 1)[-1].strip()
    if not dom or dom in _FREEMAIL:
        return ""
    return dom


def _domains_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# ── Evidence data structures ──────────────────────────────────────────────

@dataclass
class PersonEvidence:
    """What the LinkedIn profile lookup found about the *person*."""
    found: bool = False
    profile_url: str = ""
    headline: str = ""
    location: str = ""
    about: str = ""
    followers: int = 0
    work_experience_found: bool = False
    work_experience: list[str] = field(default_factory=list)  # "Title @ Company"
    current_company: str = ""
    # Companies from ACTUAL work-experience entries only — used for the
    # work-experience match flag. The headline-derived company is kept
    # separately so a headline guess never masquerades as work history.
    work_companies: list[str] = field(default_factory=list)
    headline_company: str = ""
    matches_name: bool = False
    # True when the profile looks self-authored rather than employer-verified:
    # every work entry has position == company (a placeholder, e.g.
    # "Paysfer eMart @ Paysfer eMart") and there's no About text. Such profiles
    # confirm a name, not a track record — the scorer treats them as weak legitimacy.
    self_titled: bool = False
    # True when the fetch SUCCEEDED (200) and the profile is clearly substantial
    # (has a headline + a real follower count) yet the work-experience array came
    # back EMPTY. That combination is the signature of a LinkedIn soft-throttle
    # (the expensive experience section is stripped under load) — NOT a genuinely
    # empty profile. Downstream must treat the missing work as UNVERIFIED (lower
    # confidence), never as disconfirming evidence the person is fake. This is the
    # Harpriya case: headline 'CEO @ Hotbox, backed by a16z speedrun', 2k followers,
    # work=[]. A real empty profile has ~no followers and usually no headline.
    work_unreliable: bool = False
    # high = real work experience; medium = profile found but only headline/about;
    # low/"" = no usable profile. Lets reconcile weigh incomplete-but-valid profiles.
    evidence_level: str = ""
    # Adapter outcome for this fetch (success|bad_param_repaired|not_found|
    # rate_limited|server_error|...). Surfaced so failures aren't silent.
    fetch_status: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "linkedin_profile_found": self.found,
            "linkedin_profile_matches_name": self.matches_name,
            "linkedin_headline_found": bool(self.headline),
            "linkedin_work_experience_found": self.work_experience_found,
            "linkedin_current_company": self.current_company,
            "linkedin_headline_company": self.headline_company,
            "linkedin_self_titled": self.self_titled,
            "linkedin_work_unreliable": self.work_unreliable,
            "evidence_level": self.evidence_level,
            "fetch_status": self.fetch_status,
            "profile_url": self.profile_url,
            "headline": self.headline,
            "location": self.location,
            "about": self.about,
            "followers": self.followers,
            "work_experience": list(self.work_experience),
            "work_companies": list(self.work_companies),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PersonEvidence":
        """Rehydrate from as_dict() output. Used to reload persisted raw
        enrichment so a re-run reconciles/scores the SAME evidence instead of
        re-hitting Unipile (which returns different snippets each call)."""
        d = d or {}
        def _i(v) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        return cls(
            found=bool(d.get("linkedin_profile_found", False)),
            profile_url=str(d.get("profile_url") or ""),
            headline=str(d.get("headline") or ""),
            location=str(d.get("location") or ""),
            about=str(d.get("about") or ""),
            followers=_i(d.get("followers")),
            work_experience_found=bool(d.get("linkedin_work_experience_found", False)),
            work_experience=[str(x) for x in (d.get("work_experience") or [])],
            current_company=str(d.get("linkedin_current_company") or ""),
            work_companies=[str(x) for x in (d.get("work_companies") or [])],
            headline_company=str(d.get("linkedin_headline_company") or ""),
            matches_name=bool(d.get("linkedin_profile_matches_name", False)),
            self_titled=bool(d.get("linkedin_self_titled", False)),
            work_unreliable=bool(d.get("linkedin_work_unreliable", False)),
            evidence_level=str(d.get("evidence_level") or ""),
            fetch_status=str(d.get("fetch_status") or ""),
            warnings=[str(x) for x in (d.get("warnings") or [])],
        )


@dataclass
class CompanyCandidate:
    """One possible company for the applicant. NOT yet selected as truth."""
    name: str = ""
    source: str = ""  # linkedin_company | exa_cooccurrence | submitted_url
    linkedin_url: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    location: str = ""
    follower_count: int = 0
    employee_count: str = ""
    # Structural match flags computed from person evidence + claims.
    matches_claimed_company: bool = False
    matches_person_name: bool = False
    matches_linkedin_headline: bool = False
    matches_work_experience: bool = False
    matches_submitted_domain: bool = False
    matches_email_domain: bool = False
    # Set later by the reconciler (needs event/rubric context).
    matches_luma_industry: bool = False
    matches_event_theme: bool = False
    # Set by the LLM disambiguator (disambiguate.py) ONLY in the ambiguous
    # no-hard-tie case : Claude read the full candidate descriptions and judged
    # THIS is the company the applicant actually runs/works at. Identity judgment
    # only — never event fit. reconcile rewards it but caps confidence at medium.
    matches_llm_identity: bool = False
    llm_identity_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "linkedin_url": self.linkedin_url,
            "website": self.website,
            "description": self.description,
            "industry": self.industry,
            "location": self.location,
            "follower_count": self.follower_count,
            "employee_count": self.employee_count,
            "matches_claimed_company": self.matches_claimed_company,
            "matches_person_name": self.matches_person_name,
            "matches_linkedin_headline": self.matches_linkedin_headline,
            "matches_work_experience": self.matches_work_experience,
            "matches_submitted_domain": self.matches_submitted_domain,
            "matches_email_domain": self.matches_email_domain,
            "matches_luma_industry": self.matches_luma_industry,
            "matches_event_theme": self.matches_event_theme,
            "matches_llm_identity": self.matches_llm_identity,
            "llm_identity_reason": self.llm_identity_reason,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyCandidate":
        """Rehydrate from as_dict() output (keys map 1:1 to fields)."""
        d = d or {}
        def _i(v) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        return cls(
            name=str(d.get("name") or ""),
            source=str(d.get("source") or ""),
            linkedin_url=str(d.get("linkedin_url") or ""),
            website=str(d.get("website") or ""),
            description=str(d.get("description") or ""),
            industry=str(d.get("industry") or ""),
            location=str(d.get("location") or ""),
            follower_count=_i(d.get("follower_count")),
            employee_count=str(d.get("employee_count") or ""),
            matches_claimed_company=bool(d.get("matches_claimed_company", False)),
            matches_person_name=bool(d.get("matches_person_name", False)),
            matches_linkedin_headline=bool(d.get("matches_linkedin_headline", False)),
            matches_work_experience=bool(d.get("matches_work_experience", False)),
            matches_submitted_domain=bool(d.get("matches_submitted_domain", False)),
            matches_email_domain=bool(d.get("matches_email_domain", False)),
            matches_luma_industry=bool(d.get("matches_luma_industry", False)),
            matches_event_theme=bool(d.get("matches_event_theme", False)),
            matches_llm_identity=bool(d.get("matches_llm_identity", False)),
            llm_identity_reason=str(d.get("llm_identity_reason") or ""),
            warnings=[str(x) for x in (d.get("warnings") or [])],
        )


@dataclass
class RawEvidence:
    """Everything enrichment collected for one applicant, pre-reconciliation."""
    person: PersonEvidence = field(default_factory=PersonEvidence)
    company_candidates: list[CompanyCandidate] = field(default_factory=list)
    raw_searches: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "person_evidence": self.person.as_dict(),
            "company_candidates": [c.as_dict() for c in self.company_candidates],
            "raw_searches": list(self.raw_searches),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RawEvidence":
        """Rehydrate the full raw-evidence tree from persisted JSON so a re-run
        reconciles/scores frozen evidence rather than re-enriching. This is what
        makes the inbound triage path reproducible: enrich once, persist, reuse."""
        d = d or {}
        return cls(
            person=PersonEvidence.from_dict(d.get("person_evidence") or {}),
            company_candidates=[
                CompanyCandidate.from_dict(c)
                for c in (d.get("company_candidates") or [])
            ],
            raw_searches=list(d.get("raw_searches") or []),
        )

    def is_empty(self) -> bool:
        return not (self.person.found or self.company_candidates)


# ── Unipile : person profile ──────────────────────────────────────────────

def _fetch_person_unipile(linkedin_url: str,
                          person_name: str) -> tuple[PersonEvidence, dict, ProfileResult]:
    """Fetch a LinkedIn profile via the Unipile adapter (bounded repair).

    Returns (PersonEvidence, raw_json, ProfileResult). The ProfileResult carries
    the fetch status so the caller can people-search on 422, back off on 429, and
    log the full attempt trail. A 200 with empty work-experience is a *valid but
    incomplete* result (evidence_level=medium), not a failure."""
    ev = PersonEvidence()
    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    slug = _linkedin_slug(linkedin_url)

    # Pace every account fetch_profile pulls (initial + its own 404/429 rotations)
    # so no single account is fired faster than its rate budget. Keyed by account,
    # so a bigger pool = proportionally more throughput.
    def _paced_next() -> str | None:
        aid = _next_account_id()
        _pace_account(aid)
        return aid

    result = unipile_adapter.fetch_profile(dsn, api_key, _paced_next, slug)

    # Soft-throttle repair: LinkedIn often returns a 200 with the EXPERIENCE
    # section stripped when an account is under sustained load. That looks like
    # 'success' but yields empty work history, which starves founders of
    # corroboration (their companies are small/unknown, so without work-exp their
    # confidence gets capped — the Harpriya failure).
    #
    # An immediate in-line retry only helps when there is ANOTHER account to
    # rotate to: re-hitting the SAME hot account just burns its budget harder and
    # almost always returns the same stripped body. So we only retry-in-place when
    # the pool has >1 account. With a single account we DON'T retry here — the real
    # recovery is the spaced second pass in evaluate_all (cooldown + low
    # concurrency), which gives the account time to actually recover.
    _pool_size = len(_unipile_account_ids())
    _empty_exp_retries = (int(os.environ.get("UNIPILE_EMPTY_EXP_RETRIES", "2"))
                          if _pool_size > 1 else 0)
    _attempt = 0
    while (result.ok and not (result.body or {}).get("work_experience")
           and _attempt < _empty_exp_retries):
        _attempt += 1
        retry = unipile_adapter.fetch_profile(dsn, api_key, _paced_next, slug)
        if retry.ok and (retry.body or {}).get("work_experience"):
            print(f"  [unipile.profile] {slug} → empty work-exp repaired on "
                  f"retry {_attempt}")
            result = retry
            break
        # keep the best (a still-ok body) so we don't downgrade a 200 to nothing
        if retry.ok:
            result = retry

    ev.fetch_status = result.status
    if not result.ok:
        print(f"  [unipile.profile] {slug} → {result.status} "
              f"(http={result.http_status})")
        return ev, {}, result
    d = result.body

    ev.headline = (d.get("headline") or "").strip()
    ev.location = (d.get("location") or "").strip()
    ev.about = (d.get("summary") or d.get("about") or "").strip()[:400]
    try:
        ev.followers = int(d.get("follower_count") or 0)
    except (TypeError, ValueError):
        ev.followers = 0
    ev.profile_url = (d.get("public_profile_url") or d.get("profile_url")
                      or linkedin_url or "")

    self_titled_entries = 0   # position == company (placeholder, not a real title)
    real_title_entries = 0    # a genuine distinct title under a company
    for exp in (d.get("work_experience") or [])[:6]:
        if exp is None:
            continue
        # Unipile fields are `position` / `company` (not title/company_name).
        t = (exp.get("position") or exp.get("title") or "").strip()
        c = (exp.get("company") or exp.get("company_name") or "").strip()
        if t or c:
            ev.work_experience.append(f"{t} @ {c}".strip(" @"))
            if c and len(ev.work_companies) < 4:
                ev.work_companies.append(c)
            if t and c and t.lower() == c.lower():
                self_titled_entries += 1
            elif t and c:
                real_title_entries += 1
    ev.work_experience_found = bool(ev.work_companies)
    # Self-titled/thin: has work entries, ALL are placeholders (position==company),
    # and no About text. Confirms identity but not a verified track record.
    ev.self_titled = (self_titled_entries > 0 and real_title_entries == 0
                      and not ev.about)
    if ev.work_companies:
        ev.current_company = ev.work_companies[0]

    # Parse company from headline. e.g. "Co-Founder & CEO @ Kyndred | ..." →
    # "Kyndred". Kept separate from work_companies : a headline guess must not
    # masquerade as verified work history when matching company candidates.
    if " @ " in ev.headline:
        after_at = ev.headline.split(" @ ", 1)[1]
        company_from_headline = after_at.split("|")[0].split("·")[0].strip()
        if company_from_headline:
            ev.headline_company = company_from_headline
            if not ev.current_company:
                ev.current_company = company_from_headline
            print(f"  [unipile.profile] headline company: '{company_from_headline}'")

    # Name match: tolerant of nickname/abbreviation forms ('Jeff L.' vs
    # 'Jeffrey Li') so abbreviated profiles aren't falsely flagged a mismatch.
    ev.matches_name = _name_matches(
        person_name, d.get("name", ""), d.get("first_name", ""), d.get("last_name", ""))

    ev.found = bool(ev.headline or ev.about or ev.work_companies or ev.headline_company)
    # Evidence level: real work history = high; profile w/o work-exp = medium
    # (valid but incomplete — reconcile leans on headline/about/Luma instead).
    if ev.work_experience_found:
        ev.evidence_level = "high"
    elif ev.found:
        ev.evidence_level = "medium"
    else:
        ev.evidence_level = "low"
    if ev.found and not ev.matches_name:
        ev.warnings.append("profile_name_mismatch")
    # Soft-throttle signature: a successful 200 whose work-exp array is empty,
    # yet the profile is plainly substantial (has a headline AND a real follower
    # count). That is the Harpriya case — the experience section was stripped
    # under load, NOT genuinely absent. Mark it UNRELIABLE so downstream treats
    # missing work as an unverified data gap (lower confidence) instead of as
    # disconfirming evidence (manual_review / tanked legitimacy). A real empty
    # profile has ~no followers and usually no headline, so it won't trip this.
    ev.work_unreliable = (
        ev.found and not ev.work_experience_found
        and bool(ev.headline) and ev.followers >= 30)
    if ev.found and not ev.work_experience_found:
        if ev.work_unreliable:
            ev.warnings.append("work_experience_throttle_stripped")
        else:
            ev.warnings.append("no_work_experience")
    if ev.self_titled:
        ev.warnings.append("self_titled_profile")
    print(f"  [unipile.profile] {slug} → {'ok' if ev.found else 'empty'} "
          f"[{ev.evidence_level}] (work={ev.work_companies}, headline={ev.headline_company!r})")
    return ev, d, result


def _fetch_company_unipile(company_id: str) -> dict:
    """Fetch a full LinkedIn company profile via Unipile."""
    dsn, api_key, account_id = _unipile_dsn(), _unipile_api_key(), _next_account_id()
    if not (dsn and api_key and account_id and company_id):
        return {}
    try:
        r = httpx.get(
            f"{dsn}/api/v1/linkedin/company/{company_id}",
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            params={"account_id": account_id},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _company_from_unipile(profile: dict, hit: dict, source: str) -> CompanyCandidate:
    """Build a CompanyCandidate from a Unipile company profile + search hit."""
    name = profile.get("name") or hit.get("name", "")
    industry = profile.get("industry") or hit.get("industry", "")
    if isinstance(industry, list):
        industry = ", ".join(str(x) for x in industry if x)
    locations = profile.get("locations") or []
    loc = ""
    if locations:
        h = next((l for l in locations if l.get("is_headquarter")), locations[0])
        loc = f"{h.get('city','')} {h.get('area','')} {h.get('country','')}".strip()
    try:
        followers = int(profile.get("follower_count") or hit.get("follower_count") or 0)
    except (TypeError, ValueError):
        followers = 0
    return CompanyCandidate(
        name=name,
        source=source,
        linkedin_url=profile.get("profile_url") or hit.get("profile_url") or "",
        website=(profile.get("website") or "").strip(),
        description=(profile.get("description") or hit.get("summary") or "").strip()[:600],
        industry=str(industry),
        location=loc,
        follower_count=followers,
        employee_count=str(profile.get("employee_count") or ""),
    )


def _search_company_candidates(name: str,
                               submitted_domain: str) -> tuple[list[CompanyCandidate], dict]:
    """LinkedIn company search → ALL name-matching candidates as CompanyCandidate.

    Only person-INDEPENDENT flags are set here (claimed-company, submitted-domain,
    warnings) so this can run concurrently with the person fetch; person-dependent
    tagging (work-exp, headline, name, email) is applied by the caller afterward.

    The top-3 candidate profile GETs run in parallel — they're independent, and
    serializing them was the single biggest chunk of per-applicant latency."""
    dsn, api_key, account_id = _unipile_dsn(), _unipile_api_key(), _next_account_id()
    if not (dsn and api_key and account_id and name):
        return [], {}
    try:
        r = httpx.post(
            f"{dsn}/api/v1/linkedin/search",
            headers={"X-API-KEY": api_key, "Accept": "application/json",
                     "Content-Type": "application/json"},
            params={"account_id": account_id},
            json={"api": "classic", "category": "companies", "keywords": name},
            timeout=10,
        )
        if r.status_code != 200:
            return [], {"query": name, "status": r.status_code}
        payload = r.json()
        items = payload.get("items") or []
    except Exception as e:
        return [], {"query": name, "error": str(e)}

    matched = [i for i in items if _linkedin_name_match(name, i.get("name", ""))][:3]

    # Fetch the top-3 company profiles in parallel rather than one-by-one.
    from concurrent.futures import ThreadPoolExecutor
    cids = [(h.get("id") or h.get("public_identifier") or "") for h in matched]
    if any(cids):
        with ThreadPoolExecutor(max_workers=3) as ex:
            profiles = list(ex.map(lambda c: _fetch_company_unipile(c) if c else {}, cids))
    else:
        profiles = [{} for _ in matched]

    candidates: list[CompanyCandidate] = []
    for hit, profile in zip(matched, profiles):
        cand = _company_from_unipile(profile, hit, "linkedin_company")
        cand.matches_claimed_company = _linkedin_name_match(name, cand.name)
        cand_domain = _domain(cand.website)
        cand.matches_submitted_domain = bool(submitted_domain) and bool(cand_domain) and (
            submitted_domain in cand_domain or cand_domain in submitted_domain)
        _attach_warnings(cand)
        candidates.append(cand)

    return candidates, {"query": name,
                        "result_names": [i.get("name") for i in items[:6]],
                        "matched_names": [c.name for c in candidates]}


def _search_person_unipile(person_name: str, company: str) -> str:
    """Resolve an applicant with no LinkedIn URL → their profile URL via Unipile
    people search. Returns a linkedin.com/in/<slug> URL, or '' if no confident
    name match. Conservative: requires the person's name parts to match the
    result, so fuzzy near-namesakes are rejected rather than guessed."""
    dsn, api_key = _unipile_dsn(), _unipile_api_key()
    account_id = _people_search_account_id()
    if not (dsn and api_key and account_id and person_name):
        return ""
    keywords = f"{person_name} {company}".strip()
    try:
        r = httpx.post(
            f"{dsn}/api/v1/linkedin/search",
            headers={"X-API-KEY": api_key, "Accept": "application/json",
                     "Content-Type": "application/json"},
            params={"account_id": account_id},
            json={"api": "classic", "category": "people", "keywords": keywords},
            timeout=12,
        )
        if r.status_code != 200:
            print(f"  [unipile.people] {person_name!r} → {r.status_code}")
            return ""
        items = r.json().get("items") or []
    except Exception as e:
        print(f"  [unipile.people] {person_name!r} → error: {e}")
        return ""

    parts = _person_name_parts(person_name)
    need = 2 if len(parts) >= 2 else 1   # multi-part names must match ≥2 tokens
    company_toks = _name_tokens(company)
    best, best_score = None, 0
    for it in items:
        rname = (it.get("name") or "").lower()
        name_hits = sum(1 for p in parts if p in rname)
        if name_hits < need:
            continue
        score = name_hits
        headline = (it.get("headline") or "").lower()
        if company_toks and any(t in headline for t in company_toks):
            score += 2
        if score > best_score:
            best, best_score = it, score

    # Require strong evidence: ≥2 name-token matches, OR a single-token name
    # backed by a company mention in the headline. A lone first-name hit (score
    # 1) is too ambiguous to trust.
    if best_score < 2:
        best = None
    if not best:
        print(f"  [unipile.people] {person_name!r} → no confident match "
              f"({len(items)} results)")
        return ""
    slug = (best.get("public_identifier") or "").strip()
    if not slug:
        return ""
    url = f"https://www.linkedin.com/in/{slug}"
    print(f"  [unipile.people] {person_name!r} → resolved {url} "
          f"(score={best_score}, headline={(best.get('headline') or '')[:50]!r})")
    return url


def _attach_warnings(cand: CompanyCandidate) -> None:
    if cand.follower_count and cand.follower_count < 25:
        cand.warnings.append("low_follower_count")
    if not cand.website:
        cand.warnings.append("no_website")
    if not cand.location:
        cand.warnings.append("no_location")


# ── Exa : co-occurrence + direct fetch ────────────────────────────────────

def _exa_cooccurrence(person_name: str, company_name: str,
                      submitted_domain: str) -> tuple[CompanyCandidate | None, dict]:
    """Exa 'person company founder' search → the page mentioning both.

    Returns (candidate_or_None, raw_results). The candidate is the verified
    company site when the person's name actually appears in the page text."""
    if not exa_agent.exa_available() or not person_name or not company_name:
        return None, {}
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": exa_agent._api_key(),
                         "content-type": "application/json",
                         "accept": "application/json"},
                json={"query": f"{person_name} {company_name} founder",
                      "type": "neural", "numResults": 3,
                      "contents": {"text": True}},
            )
    except Exception as e:
        return None, {"query": f"{person_name} {company_name} founder", "error": str(e)}
    if resp.status_code >= 400:
        return None, {"query": f"{person_name} {company_name} founder",
                      "status": resp.status_code}
    try:
        results = resp.json().get("results") or []
    except Exception:
        return None, {}

    raw = {"query": f"{person_name} {company_name} founder",
           "results": [{"url": r.get("url"), "title": r.get("title")} for r in results]}

    tokens = _name_tokens(company_name)
    name_parts = _person_name_parts(person_name)
    for r in results:
        url = r.get("url") or ""
        text = (r.get("text") or "").strip()[:1500]
        text_lower = text.lower()
        dom = _domain(url)
        if tokens and not any(t in dom + " " + text_lower[:300] for t in tokens):
            continue
        person_in_page = any(p in text_lower for p in name_parts)
        cand = CompanyCandidate(
            name=company_name,
            source="exa_cooccurrence",
            website=url,
            description=text[:600],
            matches_claimed_company=True,
            matches_person_name=person_in_page,
            matches_submitted_domain=bool(submitted_domain) and bool(dom) and (
                submitted_domain in dom or dom in submitted_domain),
        )
        if not person_in_page:
            cand.warnings.append("no_person_company_cooccurrence")
        print(f"  [exa.cooccurrence] '{person_name}' @ '{company_name}' → {url} "
              f"(person_in_page={person_in_page})")
        return cand, raw
    return None, raw


def _exa_direct(website: str) -> tuple[CompanyCandidate | None, dict]:
    """Direct fetch of an applicant-submitted website via Exa."""
    if not website or not exa_agent.exa_available():
        return None, {}
    snippet = exa_agent.fetch_url_snippet(website)
    raw = {"query": f"direct:{website}", "fetched": bool(snippet)}
    if not snippet:
        return None, raw
    cand = CompanyCandidate(
        name=_domain(website) or website,
        source="submitted_url",
        website=website,
        description=snippet[:600],
        matches_submitted_domain=True,
    )
    return cand, raw


# ── Main entry point ──────────────────────────────────────────────────────

async def enrich_applicant(applicant, claims=None) -> RawEvidence:
    """Collect raw evidence for one applicant. Returns candidates, not a verdict.

    Runs the Unipile person lookup and the Exa company path concurrently, then
    runs the Unipile company search (which needs the person's known_companies to
    tag work-exp matches). Every source hit becomes a CompanyCandidate; nothing
    is selected or merged here.
    """
    import asyncio

    linkedin_url = (getattr(applicant, "linkedin_url", None) or "").strip()
    website      = (getattr(applicant, "website",      None) or "").strip()
    company      = (getattr(applicant, "company",      None) or "").strip()
    person_name  = (getattr(applicant, "name",         None) or "").strip()
    # claims may carry a company the canonical field missed.
    if not company and claims is not None:
        company = (getattr(claims, "claimed_company", "") or "").strip()

    submitted_domain = _domain(website)
    email_domain     = _email_domain(getattr(applicant, "email", "") or "")

    # ── Phase 1: person profile (with people-search fallback) + Exa, concurrent ──
    # The _person coroutine owns the full resolution chain:
    #   no URL          → people-search → fetch
    #   URL but 422     → people-search → re-fetch (slug was stale/wrong)
    # Bounded: people-search runs at most once per applicant.
    person_meta: dict = {"search_used": False, "fetch": None}

    async def _person() -> tuple[PersonEvidence, dict]:
        nonlocal linkedin_url
        url = linkedin_url
        if not url and person_name:
            url = await asyncio.to_thread(_search_person_unipile, person_name, company)
            if url:
                linkedin_url = url
                person_meta["search_used"] = True
                COUNTERS.incr("unipile_people_search_fallback")
        if not url:
            return PersonEvidence(), {}
        ev, raw, result = await asyncio.to_thread(_fetch_person_unipile, url, person_name)
        person_meta["fetch"] = result
        # 422 = slug/identity issue → resolve a fresh URL once and re-fetch.
        if result.should_people_search and not person_meta["search_used"] and person_name:
            url2 = await asyncio.to_thread(_search_person_unipile, person_name, company)
            if url2 and url2 != url:
                linkedin_url = url2
                person_meta["search_used"] = True
                COUNTERS.incr("unipile_people_search_fallback")
                ev, raw, result = await asyncio.to_thread(
                    _fetch_person_unipile, url2, person_name)
                person_meta["fetch"] = result
        return ev, raw

    async def _exa() -> tuple[CompanyCandidate | None, dict]:
        if website:
            return await asyncio.to_thread(_exa_direct, website)
        if company and person_name:
            return await asyncio.to_thread(_exa_cooccurrence, person_name, company,
                                           submitted_domain)
        return None, {}

    async def _company() -> tuple[list[CompanyCandidate], dict]:
        # Person-independent: runs concurrently with the person fetch. The
        # person-dependent flags (work-exp/headline/name/email) are tagged after.
        if not company:
            return [], {}
        return await asyncio.to_thread(_search_company_candidates, company, submitted_domain)

    (person, person_raw), (exa_cand, exa_raw), (li_cands, li_raw) = \
        await asyncio.gather(_person(), _exa(), _company())

    if person_meta["search_used"] and person.found:
        # Profile came from name search, not a self-submitted URL — mark it so
        # the reconciler/scorer treats identity as slightly less certain.
        person.warnings.append("linkedin_resolved_via_search")

    raw_searches: list[dict] = []
    if person_meta["search_used"]:
        raw_searches.append({"source": "unipile_people_search",
                             "raw": {"query": f"{person_name} {company}".strip(),
                                     "resolved_url": linkedin_url}})
    if person_meta["fetch"] is not None:
        raw_searches.append({"source": "unipile_profile_fetch",
                             "raw": person_meta["fetch"].debug_dict()})
    if person_raw:
        raw_searches.append({"source": "unipile_profile", "raw": _trim_raw(person_raw)})
    if exa_raw:
        raw_searches.append({"source": "exa", "raw": exa_raw})

    # ── Assemble candidates + apply person-dependent tagging ─────────────
    candidates: list[CompanyCandidate] = list(li_cands)
    if li_raw:
        raw_searches.append({"source": "unipile_company_search", "raw": li_raw})
    if exa_cand is not None:
        candidates.append(exa_cand)

    name_parts = _person_name_parts(person_name)
    work = [c.lower() for c in person.work_companies]
    headline_lower = person.headline.lower()
    for c in candidates:
        cn = c.name.lower()
        if c.source == "linkedin_company":
            if name_parts:
                c.matches_person_name = any(p in c.description.lower() for p in name_parts)
            c.matches_work_experience = bool(cn) and any(k in cn or cn in k for k in work)
            c.matches_linkedin_headline = bool(cn) and cn in headline_lower
        # Email-domain ↔ company-website match : co-occurrence-equivalent evidence.
        if email_domain and c.website:
            c.matches_email_domain = _domains_match(_domain(c.website), email_domain)
        if c.source != "exa_cooccurrence" and "no_person_company_cooccurrence" not in c.warnings \
                and not c.matches_person_name and not c.matches_email_domain:
            c.warnings.append("no_person_company_cooccurrence")

    return RawEvidence(person=person, company_candidates=candidates,
                       raw_searches=raw_searches)


def _trim_raw(d: dict) -> dict:
    """Keep the debug artifact small : drop bulky/nested fields we don't need."""
    keep = ("headline", "location", "summary", "about", "follower_count",
            "name", "first_name", "last_name", "public_profile_url",
            "work_experience", "occupation")
    return {k: d.get(k) for k in keep if k in d}
