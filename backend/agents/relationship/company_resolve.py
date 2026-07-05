"""agents/relationship/company_resolve.py : person -> company resolution.

The account layer (docs/accounts-architecture.md, section 3) needs one thing
before anything else can exist: a reliable answer to "which Company does this
Contact work at?". This module is that resolver, same philosophy as
identity.py one level up: STRONG keys auto-link deterministically, WEAK
signals (a bare company-name string) go through disambiguation and land below
1.0 confidence -- or in pending_review when we cannot be sure.

WHY TWO PATHS
  - STRONG (confidence 1.0): a non-freemail domain -- `contact.company_domain`
    or the domain of `contact.email` -- is a globally unique key. Exact
    CompanyIdentity(kind="domain") lookup either links to the existing global
    Company or mints a new one. No LLM, no ambiguity, ever.
  - NAME (confidence <= 0.9): a company-name string, from `contact.company`
    or extracted out of `contact.headline`. Measured on prod only 48/3202
    contacts carry a domain while 91% carry a LinkedIn headline, so headline
    extraction is the PRIMARY employer source, not a nicety. Names collide
    across real companies (the Brittany/Kyndred bug class), so a name NEVER
    auto-merges two Company rows: an exact-and-UNAMBIGUOUS name_norm match
    links at 0.9, anything ambiguous goes to the LLM disambiguator, and when
    the LLM is unavailable or unsure the membership lands as
    status="pending_review" pointing at the best candidate for a human to
    confirm -- the same pattern as medium-confidence person merges.

HEADLINE EXTRACTION is deterministic-first: most LinkedIn headlines are
"Title at Company | vanity segments", so a couple of regexes ("at X" / "@ X",
capture stopping at the segment separators) cover the bulk for free. Only
when the patterns fail AND an ANTHROPIC_API_KEY is configured do we ask the
model; with no key the whole module still works end to end (tests run keyless).

IDEMPOTENT BY CONSTRUCTION: re-resolving a contact never duplicates a
membership. A differing CURRENT membership is closed (ended_at=now,
is_current=False) before the new one opens -- the exact close/open semantic
the job-change hook needs, exposed directly as close_and_reopen_membership().
Every successful resolution also lazily materializes the owner's Account row
(owner_type="user") and refreshes its cached contact_count rollup.

Nothing here commits except backfill(dry_run=False); callers own the
transaction, and backfill(dry_run=True) rolls back so a report costs nothing.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from ... import models
from ...models import _utcnow


# ── LLM plumbing (graceful, key-optional -- same pattern as book.py) ─────────

def _anthropic_available() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def _llm_json(system: str, user: str, *, max_tokens: int = 400,
              cheap: bool = True) -> Optional[dict]:
    """Call Claude in JSON mode and parse the first JSON object out of the
    reply. Resolution calls are triage-shaped, so they default to the cheap
    model and run as background through the shared rate gate (they must never
    starve a foreground /ask or /draft). Returns None on ANY failure (no key,
    SDK missing, rate limit, unparseable) so every caller falls back to its
    deterministic path. Never raises."""
    if not _anthropic_available():
        return None
    t0 = time.monotonic()
    try:
        from .. import llm, rategate
        with rategate.gate(background=True):
            resp = llm._client().messages.create(
                model=(llm.JUDGE_MODEL if cheap else llm.MODEL),
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        return json.loads(text[start:end + 1])
    except Exception as exc:  # noqa: BLE001 : LLM is best-effort, fall back
        print(f"[company_resolve] llm ERR in {time.monotonic() - t0:.1f}s: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


# ── normalization : a key only means "same company" if normalized ────────────

# Free / generic email providers. An address here says NOTHING about the
# person's employer, so a freemail domain must NEVER become a company identity
# (imagine every gmail.com contact merged into one giant "Gmail" account).
# Superset of triage/enrich.py's _FREEMAIL, kept local so the account layer
# has no import edge into triage.
FREEMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.net", "mail.com", "email.com", "hey.com", "fastmail.com",
    "zoho.com", "qq.com", "163.com", "126.com", "duck.com", "yandex.com",
    "yandex.ru", "mail.ru", "hotmail.co.uk", "hotmail.fr", "yahoo.co.uk",
    "yahoo.fr", "yahoo.ca", "yahoo.co.in", "comcast.net", "verizon.net",
    "att.net", "sbcglobal.net", "cox.net", "earthlink.net", "web.de",
    "t-online.de", "orange.fr", "free.fr", "naver.com", "example.com",
})

# Trailing legal-form tokens stripped by normalize_company_name so
# "Acme, Inc." / "Acme LLC" / "Acme Corp" all key as "acme". Deliberately
# EXCLUDES words like "group"/"holdings"/"labs" that are part of the brand
# ("Blackstone Group" is not "Blackstone" -- collapsing those would mis-merge).
_LEGAL_SUFFIXES: frozenset[str] = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "pllc", "pc", "gmbh", "ag", "sa",
    "sarl", "srl", "bv", "nv", "ab", "oy", "as", "kk", "pty", "pte",
})


def normalize_domain(url_or_domain: Optional[str]) -> str:
    """Canonical lowercase registrable-ish domain from a URL, bare domain, or
    even an email address. Strips scheme, credentials, leading "www.", port,
    path/query/fragment, and trailing dots. Returns '' when no plausible
    domain is derivable -- '' is always "no signal", never a key."""
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "@" in s:  # tolerate a full email being passed in
        s = s.rsplit("@", 1)[-1]
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)   # scheme
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split(":", 1)[0].strip().strip(".")
    if s.startswith("www."):
        s = s[4:]
    if "." not in s or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", s):
        return ""
    return s


def is_freemail(domain: str) -> bool:
    """True when the (already normalized) domain is a free/generic email
    provider and therefore carries zero employer signal."""
    return bool(domain) and domain in FREEMAIL_DOMAINS


def normalize_company_name(name: Optional[str]) -> str:
    """Weak-key normal form for a company name: lowercase, punctuation folded
    to spaces, trailing legal suffixes (inc/llc/ltd/corp/...) dropped,
    whitespace collapsed. "Acme, Inc." == "acme  LLC" == "acme". Never strips
    a suffix that IS the whole name ("Inc" stays "inc"). Returns '' for no
    usable name."""
    s = (name or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


# ── headline -> employer extraction (deterministic first, LLM fallback) ──────

# Most headlines read "Title at Company | more | segments" or "Title @Company".
# Capture stops at the LinkedIn segment separators so vanity tails never leak
# into the name. The "at" pattern runs first ("CTO at Acme"), then "@".
_AT_PATTERN = re.compile(r"\bat\s+([^|•·;,]+)", re.IGNORECASE)
_AMP_PATTERN = re.compile(r"@\s*([^|•·;,]+)")

# Captures that are grammar, not employers ("working at scale", "at the
# intersection of x and y", stealth-mode people). Compared post-normalization.
_NOT_EMPLOYERS: frozenset[str] = frozenset({
    "scale", "heart", "night", "home", "work", "large", "stealth",
    "stealth startup", "a stealth startup", "stealth mode",
})

_EXTRACT_SYSTEM = (
    "You extract the CURRENT employer from a LinkedIn headline. Return ONLY "
    "JSON, no prose: {\"employer\": \"<company name>\" | null}. null when the "
    "headline names no employer (freelancers, students, slogans). Return the "
    "company name exactly as written, without titles or taglines."
)


def _clean_employer(raw: str) -> Optional[str]:
    """Trim a regex capture down to a plausible company name, or None. Splits
    off a spaced-dash tail ("Acme - building the future" -> "Acme") without
    touching in-name hyphens ("Coca-Cola" survives)."""
    s = re.split(r"\s+[–-]\s+", (raw or "").strip())[0].strip(" .,;:!·")
    if len(s) < 2 or len(s) > 80:
        return None
    if normalize_company_name(s) in _NOT_EMPLOYERS:
        return None
    # A lowercase-as-written blocklist word opening the capture is grammar
    # ("working at scale on hard problems"), while a capitalized one is a
    # brand ("at Scale AI") -- casing is the only signal that separates them.
    first = s.split()[0]
    if first.islower() and first in _NOT_EMPLOYERS:
        return None
    return s


def extract_employer_from_headline(headline: Optional[str],
                                   allow_llm: bool = True) -> Optional[str]:
    """Company name out of a LinkedIn headline, or None.

    Deterministic patterns first (free, instant, cover the common "Title at
    Company | ..." shape); the LLM only sees headlines the patterns cannot
    parse, and only when a key is configured -- keyless environments simply
    treat pattern-less headlines as no signal."""
    h = (headline or "").strip()
    if not h:
        return None
    for pattern in (_AT_PATTERN, _AMP_PATTERN):
        m = pattern.search(h)
        if m:
            cand = _clean_employer(m.group(1))
            if cand:
                return cand
    if allow_llm and _anthropic_available():
        out = _llm_json(_EXTRACT_SYSTEM, f"Headline: {h[:300]}")
        if out and isinstance(out.get("employer"), str):
            return _clean_employer(out["employer"])
    return None


# ── company find-or-create ───────────────────────────────────────────────────

def _follow_merges(db, company) -> "models.Company":
    """Follow merged_into_id tombstones to the surviving Company row (bounded
    hop count so a cyclic tombstone can never spin)."""
    hops = 0
    while company is not None and company.merged_into_id and hops < 10:
        nxt = db.get(models.Company, company.merged_into_id)
        if nxt is None:
            break
        company, hops = nxt, hops + 1
    return company


def _company_by_identity(db, kind: str, value: str) -> Optional["models.Company"]:
    row = (db.query(models.CompanyIdentity)
             .filter_by(kind=kind, value=value)
             .one_or_none())
    if row is None:
        return None
    company = db.get(models.Company, row.company_id)
    return _follow_merges(db, company) if company else None


def _ensure_identity(db, company_id: int, kind: str, value: str,
                     *, confidence: float = 1.0, source: str = "resolver") -> None:
    """Insert a CompanyIdentity iff the (kind, value) slot is free. The slot
    is globally unique, so when another company already holds it we SKIP --
    never re-point, never merge. For name_norm this is load-bearing: two real
    companies can share a normalized name, and silently re-keying the
    identity would be exactly the auto-merge this schema forbids."""
    if not value:
        return
    existing = (db.query(models.CompanyIdentity)
                  .filter_by(kind=kind, value=value)
                  .one_or_none())
    if existing is None:
        db.add(models.CompanyIdentity(company_id=company_id, kind=kind,
                                      value=value, confidence=confidence,
                                      source=source))
        db.flush()


def _create_company(db, canonical_name: str, *, primary_domain: str = "",
                    source: str = "resolver") -> "models.Company":
    """Mint a global Company row plus its identities. name_norm is added for
    EVERY company (best-effort, slot may be taken -- see _ensure_identity);
    a domain identity only when a real domain is known."""
    company = models.Company(canonical_name=canonical_name.strip()[:200],
                             primary_domain=primary_domain or None)
    db.add(company)
    db.flush()
    if primary_domain:
        _ensure_identity(db, company.id, "domain", primary_domain, source=source)
    _ensure_identity(db, company.id, "name_norm",
                     normalize_company_name(canonical_name),
                     confidence=0.9, source=source)
    return company


def _companies_by_name_norm(db, name_norm: str) -> list["models.Company"]:
    """ALL live companies whose normalized name matches -- the ambiguity set.

    The name_norm CompanyIdentity is unique per (kind, value), so at most ONE
    company can hold the identity row; a second company with the same name
    (created via its domain) simply has no name_norm identity. An identity
    lookup alone would therefore hide real ambiguity. Company cardinality is
    small (hundreds, vs thousands of contacts), so we scan live rows and
    normalize in Python -- correctness over cleverness here."""
    seen: dict[int, models.Company] = {}
    ident = _company_by_identity(db, "name_norm", name_norm)
    if ident is not None:
        seen[ident.id] = ident
    rows = (db.query(models.Company)
              .filter(models.Company.merged_into_id.is_(None))
              .all())
    for c in rows:
        if c.id not in seen and normalize_company_name(c.canonical_name) == name_norm:
            seen[c.id] = c
    return list(seen.values())


# ── account + membership upkeep ──────────────────────────────────────────────

def _refresh_contact_count(db, user_id: int, company_id: int) -> None:
    # Flush first: sessions here may run with autoflush=False (the test
    # convention), and the recount below must see just-closed memberships.
    db.flush()
    acct = (db.query(models.Account)
              .filter_by(owner_type="user", owner_id=user_id,
                         company_id=company_id)
              .one_or_none())
    if acct is None:
        return
    acct.contact_count = (
        db.query(models.AccountMembership.contact_id)
          .filter_by(user_id=user_id, company_id=company_id, is_current=True)
          .filter(models.AccountMembership.status != "rejected")
          .distinct().count())


def _ensure_account(db, user_id: int, company_id: int) -> "models.Account":
    """Lazily materialize the owner's Account row (owner_type="user") the
    first time a resolved membership lands for this company -- the exact
    lazy-creation contract Contact has for people -- and refresh the cached
    contact_count rollup (recomputed, never incremented, so re-resolves and
    dry runs cannot drift it)."""
    acct = (db.query(models.Account)
              .filter_by(owner_type="user", owner_id=user_id,
                         company_id=company_id)
              .one_or_none())
    if acct is None:
        acct = models.Account(owner_type="user", owner_id=user_id,
                              company_id=company_id)
        db.add(acct)
        db.flush()
    _refresh_contact_count(db, user_id, company_id)
    return acct


def _upsert_membership(db, contact, company_id: int, *, source: str,
                       confidence: float, status: str,
                       via: str) -> "models.AccountMembership":
    """Idempotent membership write with job-change close/open semantics.

    - Same current company: update in place (a stronger signal may raise
      confidence or promote pending_review -> linked). Never a second row.
    - Different current company: close the old edge (is_current=False,
      ended_at=now) and open the new one -- both sides of a job move stay in
      history. EXCEPTION: a pending_review guess never displaces a current
      membership held at strictly higher confidence (a weak name match must
      not evict a domain-proven link); the existing edge is returned instead.

    The returned row carries a transient `resolved_via` attribute (not a
    column) so callers like backfill() can report which path resolved it."""
    now = _utcnow()
    current = (db.query(models.AccountMembership)
                 .filter_by(user_id=contact.user_id, contact_id=contact.id,
                            is_current=True)
                 .all())
    same = [m for m in current if m.company_id == company_id]
    if same:
        m = same[0]
        if confidence >= m.confidence:
            m.confidence = confidence
            m.source = source
            if status == "linked":
                m.status = "linked"
        _ensure_account(db, contact.user_id, company_id)
        m.resolved_via = via
        return m

    if status == "pending_review":
        stronger = [m for m in current
                    if m.status == "linked" and m.confidence > confidence]
        if stronger:
            m = stronger[0]
            m.resolved_via = via
            return m

    for m in current:  # job change: close every differing current edge
        m.is_current = False
        m.ended_at = now
        _refresh_contact_count(db, contact.user_id, m.company_id)

    m = models.AccountMembership(
        user_id=contact.user_id, contact_id=contact.id, company_id=company_id,
        role_title=(getattr(contact, "title", None) or None),
        is_current=True, started_at=now, source=source,
        confidence=confidence, status=status)
    db.add(m)
    db.flush()
    _ensure_account(db, contact.user_id, company_id)
    m.resolved_via = via
    return m


def close_and_reopen_membership(db, contact, new_company_id: int,
                                source: str = "job_change_event",
                                ) -> "models.AccountMembership":
    """The job-change hook: close the contact's current membership(s) with
    ended_at=now + is_current=False and open a new current one at
    new_company_id. Both accounts' contact_count rollups refresh, so the
    coverage drop at OldCo and the new warm path into NewCo are visible
    immediately. No-op (returns the existing row) when the contact is already
    current at new_company_id."""
    return _upsert_membership(db, contact, new_company_id, source=source,
                              confidence=1.0, status="linked",
                              via="job_change")


# ── LLM name disambiguation ──────────────────────────────────────────────────

_DISAMBIG_SYSTEM = (
    "You resolve which company a professional contact works at. You get the "
    "raw company name from their profile, context about the person, and "
    "numbered candidate companies from the database. Decide if the name "
    "refers to one of the candidates or to a different company entirely. "
    "Return ONLY JSON, no prose: {\"company_id\": <candidate id> | null, "
    "\"confidence\": 0.0-1.0}. Use null with high confidence when it is "
    "clearly a NEW company; use low confidence when you cannot tell."
)

# Below this the resolver refuses to auto-link a name match and files the
# membership as pending_review for a human instead.
AUTO_LINK_THRESHOLD = 0.75


def _llm_disambiguate(db, contact, raw_name: str,
                      candidates: list) -> Optional[dict]:
    """Ask Claude which candidate (if any) the name refers to. Returns the
    parsed {"company_id", "confidence"} dict, or None when the LLM is
    unavailable or unparseable (callers then fall to pending_review)."""
    lines = [f"Company name on profile: {raw_name}",
             f"Contact: {getattr(contact, 'name', None) or 'unknown'}; "
             f"title: {getattr(contact, 'title', None) or '-'}; "
             f"headline: {(getattr(contact, 'headline', None) or '-')[:200]}; "
             f"linkedin: {getattr(contact, 'linkedin_url', None) or '-'}",
             "Candidates:"]
    for c in candidates:
        lines.append(f"  id={c.id} name={c.canonical_name!r} "
                     f"domain={c.primary_domain or '-'} "
                     f"enrichment={(c.enrichment_json or '{}')[:150]}")
    out = _llm_json(_DISAMBIG_SYSTEM, "\n".join(lines))
    if not out:
        return None
    cid = out.get("company_id")
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return None
    if cid is not None:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return None
        if cid not in {c.id for c in candidates}:
            return None
    return {"company_id": cid, "confidence": conf}


def _best_candidate(candidates: list) -> "models.Company":
    """Deterministic pending_review target when the LLM cannot rank: prefer a
    domain-anchored company (it is at least a REAL, verified org), then the
    oldest row (stable across runs)."""
    return sorted(candidates,
                  key=lambda c: (0 if c.primary_domain else 1, c.id))[0]


# ── the resolver entry point ─────────────────────────────────────────────────

def resolve_contact(db, contact, *,
                    source: str = "enrichment",
                    allow_llm: bool = True,
                    ) -> Optional["models.AccountMembership"]:
    """Resolve one Contact to a Company and upsert its AccountMembership.

    Path order (first signal wins):
      1. STRONG: contact.company_domain, else the domain of contact.email,
         both freemail-filtered -> exact domain identity -> link or create,
         confidence 1.0.
      2. NAME: contact.company, else the employer extracted from
         contact.headline -> unambiguous name_norm match links at 0.9; a new
         name mints a new Company; an AMBIGUOUS name goes to the LLM and
         lands as pending_review when the LLM is unavailable or under the
         0.75 auto-link threshold.

    Returns the membership (transient .resolved_via in {"domain", "name",
    "headline", "job_change"}), or None when the contact carries no employer
    signal at all. Flushes but never commits; the caller owns the txn."""
    # -- 1. strong path: a non-freemail domain is a global key ---------------
    dom = normalize_domain(getattr(contact, "company_domain", None))
    if not dom or is_freemail(dom):
        email_dom = normalize_domain(getattr(contact, "email", None))
        dom = email_dom if email_dom and not is_freemail(email_dom) else ""
    if dom:
        company = _company_by_identity(db, "domain", dom)
        if company is None:
            # Prefer the human-readable name string when we have one; fall
            # back to the domain's first label so the row is presentable.
            display = ((getattr(contact, "company", None) or "").strip()
                       or dom.split(".")[0].capitalize())
            company = _create_company(db, display, primary_domain=dom,
                                      source=source)
        return _upsert_membership(db, contact, company.id, source=source,
                                  confidence=1.0, status="linked",
                                  via="domain")

    # -- 2. name path: company string, else headline extraction --------------
    raw_name = (getattr(contact, "company", None) or "").strip()
    via = "name"
    if not raw_name:
        raw_name = extract_employer_from_headline(
            getattr(contact, "headline", None), allow_llm=allow_llm) or ""
        via = "headline"
    name_norm = normalize_company_name(raw_name)
    if not name_norm:
        return None  # no employer signal anywhere: skip, never guess

    candidates = _companies_by_name_norm(db, name_norm)

    if len(candidates) == 1:
        return _upsert_membership(db, contact, candidates[0].id,
                                  source=source, confidence=0.9,
                                  status="linked", via=via)

    if not candidates:
        company = _create_company(db, raw_name, source=source)
        # A name typed on the profile is a touch stronger than one parsed out
        # of a headline; both clear the auto-link threshold.
        conf = 0.9 if via == "name" else 0.8
        return _upsert_membership(db, contact, company.id, source=source,
                                  confidence=conf, status="linked", via=via)

    # Ambiguous: 2+ live companies share this normalized name. Never
    # auto-pick deterministically (Brittany/Kyndred class of bug).
    choice = _llm_disambiguate(db, contact, raw_name, candidates) \
        if allow_llm else None
    if choice is not None and choice["company_id"] is None \
            and choice["confidence"] >= AUTO_LINK_THRESHOLD:
        company = _create_company(db, raw_name, source=source)
        return _upsert_membership(db, contact, company.id, source=source,
                                  confidence=choice["confidence"],
                                  status="linked", via=via)
    if choice is not None and choice["company_id"] is not None \
            and choice["confidence"] >= AUTO_LINK_THRESHOLD:
        return _upsert_membership(db, contact, choice["company_id"],
                                  source=source,
                                  confidence=choice["confidence"],
                                  status="linked", via=via)

    # LLM unavailable / unsure: best candidate, flagged for human review.
    target = (db.get(models.Company, choice["company_id"])
              if choice and choice["company_id"] is not None
              else _best_candidate(candidates))
    conf = choice["confidence"] if choice else 0.5
    return _upsert_membership(db, contact, target.id, source=source,
                              confidence=conf, status="pending_review",
                              via=via)


# ── backfill ─────────────────────────────────────────────────────────────────

def backfill(db, user_id: Optional[int] = None, dry_run: bool = True) -> dict:
    """Sweep existing Contacts through resolve_contact and report.

    dry_run=True (the default, matching identity.py's safety contract)
    computes everything, builds the report, then ROLLS BACK -- zero rows land,
    so the report is a free preview of what --execute would do. dry_run=False
    commits at the end. Memberships land with source="backfill" so a bad
    sweep stays auditable and reversible.

    Returns::
        {"total": n, "resolved_strong": n, "resolved_name": n,
         "pending_review": n, "skipped_no_signal": n, "companies_created": n,
         "sample": [(contact_name, company_name, via, confidence), ...]}
    """
    q = db.query(models.Contact).order_by(models.Contact.id)
    if user_id is not None:
        q = q.filter(models.Contact.user_id == user_id)
    contacts = q.all()

    companies_before = db.query(models.Company).count()
    report = {"total": len(contacts), "resolved_strong": 0,
              "resolved_name": 0, "pending_review": 0,
              "skipped_no_signal": 0, "companies_created": 0, "sample": []}

    for contact in contacts:
        membership = resolve_contact(db, contact, source="backfill")
        if membership is None:
            report["skipped_no_signal"] += 1
            continue
        via = getattr(membership, "resolved_via", "name")
        if membership.status == "pending_review":
            report["pending_review"] += 1
        elif via == "domain":
            report["resolved_strong"] += 1
        else:
            report["resolved_name"] += 1
        if len(report["sample"]) < 15:
            company = db.get(models.Company, membership.company_id)
            report["sample"].append((
                getattr(contact, "name", None) or "",
                company.canonical_name if company else "",
                via, round(membership.confidence, 2)))

    # Count before tearing anything down: rollback expires these objects.
    report["companies_created"] = (
        db.query(models.Company).count() - companies_before)

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return report
