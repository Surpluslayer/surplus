"""agents/updates_watch.py : account-safe "what's new" from PUBLIC web sources.

relationship_watch.py emits the Today-feed `activity_update` rows by polling
LinkedIn through the host's Unipile account -- which risks the account and leaves
a "viewed your profile" footprint on every contact. This module emits the SAME
rows from PUBLIC web data, so the Updates feed populates WITHOUT ever touching
LinkedIn or the host's account. Three legs, each best-effort:

    news  -- Google News RSS (no key, no cost): funding/launch/press coverage,
             i.e. what a person finds when they literally google the contact.
    web   -- Exa neural search across the open web (semantic catch-all).
    x     -- Exa scoped to x.com, sharpened by the contact's X handle when one
             can be resolved (stored as an `x_handle` ContactFact).

All legs feed ONE extraction call + the same identity gate + seen-url dedup, so
adding a source never adds an LLM call and never weakens the same-name guard.

What it can find: a role change that surfaced publicly, a fundraise, a launch /
announcement, an award, press, a notable post or talk -- recent (last ~month).
What it can't: someone's literal private LinkedIn (gated). That's the deliberate
trade for being un-bannable.

Run on a schedule: GitHub Actions -> POST /admin/run-updates (see
.github/workflows/updates.yml). Idempotent per contact via seen-url dedup.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as _ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

from ... import models
from .. import exa
from .book import _llm_json          # shared Claude->JSON helper (+ tracing)
from .relationship_watch import _emit  # writes the activity_update row


# "Last month" window for a recent update. Env-tunable.
_LOOKBACK_DAYS = max(1, int(os.environ.get("UPDATES_LOOKBACK_DAYS", "30")))
_KNOWN_KINDS = {"job_change", "new_post", "profile_update"}


def _news_enabled() -> bool:
    return (os.environ.get("UPDATES_NEWS_ENABLED", "1").strip().lower()
            not in ("0", "false", "no"))


def _x_enabled() -> bool:
    return (os.environ.get("UPDATES_X_ENABLED", "1").strip().lower()
            not in ("0", "false", "no"))

_EXTRACT_SYSTEM = (
    "You scan recent web results about ONE professional contact and decide if "
    "there is a single noteworthy recent update worth telling someone who already "
    "knows them: a role change, a fundraise, a launch/announcement, an award, "
    "press, or a notable post/talk. Ignore stale, generic, or unrelated results.\n"
    "IDENTITY IS CRITICAL: web search returns same-name DIFFERENT people. Only "
    "accept a result you can tie to THIS EXACT person -- their company, role, or "
    "context must match. `identity_confidence`=high ONLY when the result clearly "
    "corroborates this person (e.g. names their company/role); otherwise low. Set "
    "`matched_company` to the employer the result is actually about (so a mismatch "
    "with the given company is visible).\n"
    "Return ONLY JSON: {\"has_update\":true|false,\"type\":\"job_change|new_post|"
    "profile_update\",\"headline\":\"<=8 words\",\"summary\":\"<=25 words, "
    "specific, names the thing\",\"url\":\"<source url>\","
    "\"identity_confidence\":\"high|low\",\"matched_company\":\"<employer in the result>\"}. "
    "Use job_change for a new role/company, new_post for a post/launch/press/talk, "
    "profile_update otherwise. has_update=false unless the evidence is solid, recent, "
    "AND clearly about this exact person."
)


# Generic company words that don't distinguish one employer from another -- a match
# on these alone doesn't corroborate identity.
_GENERIC_COMPANY_WORDS = {
    "inc", "llc", "ltd", "co", "corp", "company", "group", "the", "and",
    "technologies", "technology", "tech", "labs", "lab", "ai", "io", "capital",
    "ventures", "partners", "global", "solutions", "systems", "security",
    "services", "studio", "studios", "holdings", "ventures",
}


def _norm_text(s: str) -> str:
    """Lowercased, alnum-or-space, single-spaced -- for substring identity checks."""
    return " ".join("".join(c if (c.isalnum() or c.isspace()) else " "
                            for c in (s or "").lower()).split())


def _result_blob(out: dict, packed: list[dict]) -> str:
    """Normalized text of the SPECIFIC result the extractor chose (by url); falls
    back to all results' text if the url isn't among them."""
    url = (out.get("url") or "").strip()
    chosen = next((p for p in packed if (p.get("url") or "") == url), None)
    src = ([chosen] if chosen else packed)
    return _norm_text(" ".join((p.get("title") or "") + " " + (p.get("text") or "")
                               for p in src))


def _identity_ok(out: dict, packed: list[dict], name: str, company: str) -> bool:
    """Deterministic identity gate for an Exa web hit -- the fix for same-name
    mis-attribution. Requires the extractor's own high confidence AND, when we know
    the contact's company, that the chosen result actually corroborates it (full
    company string OR a distinctive company token present). No company on file ->
    fall back to requiring the contact's name in the result."""
    if (out.get("identity_confidence") or "").strip().lower() != "high":
        return False
    blob = _result_blob(out, packed)
    if not blob:
        return False
    comp = _norm_text(company)
    if comp:
        if comp in blob:
            return True
        tokens = [t for t in comp.split()
                  if len(t) >= 4 and t not in _GENERIC_COMPANY_WORDS]
        return any(t in blob for t in tokens)   # company known but uncorroborated -> reject
    return _norm_text(name) in blob              # no company: name must at least appear


def _exa_search(query: str, *, lookback_days: int = _LOOKBACK_DAYS,
                n: int = 6, include_domains: list[str] | None = None) -> list[dict]:
    """Recent web results for `query` via Exa (newest-leaning, with text snippets).
    `include_domains` scopes the index (e.g. ["x.com"] for the X leg). Returns []
    when Exa isn't configured or on any failure -- best-effort only."""
    if not exa.exa_available():
        return []
    since = (datetime.now(timezone.utc)
             - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    body: dict = {"query": query, "numResults": n, "type": "auto",
                  "startPublishedDate": since,
                  "contents": {"text": {"maxCharacters": 600}}}
    if include_domains:
        body["includeDomains"] = include_domains
    try:
        r = httpx.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": exa._api_key(), "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        if r.status_code >= 300:
            return []
        return (r.json() or {}).get("results") or []
    except Exception:  # noqa: BLE001 : web lookup is best-effort
        return []


# Google News RSS is an unofficial-but-stable endpoint; identify ourselves and
# keep the volume polite (one GET per contact per sweep).
_NEWS_UA = {"User-Agent": "Mozilla/5.0 (compatible; surplus-updates/1.0)"}


def _google_news_search(query: str, *, lookback_days: int = _LOOKBACK_DAYS,
                        n: int = 6) -> list[dict]:
    """Recent Google News results for `query` -- the "what people find when they
    google them" leg (BetaKit/TechCrunch raise coverage, press, local news).
    Free, keyless RSS; returns the SAME packed shape as the other legs so the
    extractor is source-agnostic. Best-effort: [] on any failure."""
    q = (query or "").strip()
    if not q:
        return []
    url = ("https://news.google.com/rss/search?q="
           + quote_plus(f"{q} when:{lookback_days}d")
           + "&hl=en-US&gl=US&ceid=US:en")
    try:
        r = httpx.get(url, headers=_NEWS_UA, timeout=15, follow_redirects=True)
        if r.status_code >= 300:
            return []
        root = _ET.fromstring(r.text)
    except Exception:  # noqa: BLE001 : news lookup is best-effort
        return []
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        published = (item.findtext("pubDate") or "").strip()
        # `when:Nd` already filters server-side; re-check locally because the
        # operator has flipped silently before and stale news reads as current.
        if published:
            try:
                if parsedate_to_datetime(published) < since:
                    continue
            except Exception:  # noqa: BLE001 : unparseable date -> keep the item
                pass
        # Descriptions are HTML (<a href=...>title</a> + source name); strip tags.
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        source = (item.findtext("source") or "").strip()
        text = " ".join(f"{source}: {desc}".split())[:500]
        out.append({"title": title, "url": link, "published": published or None,
                    "text": text, "via": "news"})
        if len(out) >= n:
            break
    return out


_X_PROFILE_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})/?(?:$|\?)", re.I)
# x.com paths that are site chrome, not user profiles.
_X_NON_HANDLES = {"i", "home", "search", "explore", "hashtag", "intent",
                  "share", "notifications", "messages", "settings", "login"}


def _resolve_x_handle(db, contact: models.Contact) -> str:
    """The contact's X handle: from the `x_handle` ContactFact when already
    resolved, else one Exa profile lookup scoped to x.com (stored on success so
    resolution costs once per contact, not once per sweep). "" when unknown."""
    row = (db.query(models.ContactFact)
             .filter_by(contact_id=contact.id, key="x_handle")
             .one_or_none())
    if row is not None:
        return (row.value or "").strip()
    name = (contact.name or "").strip()
    company = (contact.company or "").strip()
    company = "" if company.lower() in ("", "unknown") else company
    handle = ""
    for hit in _exa_search(f"{name} {company}".strip() + " profile",
                           lookback_days=3650, n=5, include_domains=["x.com"]):
        m = _X_PROFILE_RE.search(hit.get("url") or "")
        if not m or m.group(1).lower() in _X_NON_HANDLES:
            continue
        # Same-name collision guard: the profile page must mention the contact's
        # name (and company when we know one distinctive token of it).
        blob = _norm_text((hit.get("title") or "") + " " + (hit.get("text") or ""))
        if _norm_text(name) not in blob:
            continue
        handle = m.group(1)
        break
    # Store even the miss ("" value): a contact with no findable X presence should
    # not re-pay the resolution search every sweep. upsert refreshes observed_at.
    try:
        from .spine.memory import upsert_fact
        upsert_fact(db, contact.user_id, contact.id, "x_handle", handle,
                    source="enrichment",
                    confidence="high" if handle else "low", commit=False)
    except Exception:  # noqa: BLE001 : caching the handle is best-effort
        pass
    return handle


def _seen_urls(contact: models.Contact) -> set[str]:
    try:
        return set(json.loads(contact.seen_post_ids or "[]"))
    except Exception:  # noqa: BLE001
        return set()


def _pack_exa(results: list[dict], via: str, n: int = 6) -> list[dict]:
    """Exa results -> the source-agnostic packed shape the extractor reads."""
    return [{"title": x.get("title"), "url": x.get("url"),
             "published": x.get("publishedDate"),
             "text": (x.get("text") or "")[:500], "via": via}
            for x in results[:n]]


def _gather_candidates(db, contact: models.Contact, name: str,
                       company: str) -> list[dict]:
    """All source legs for one contact, merged + deduped by URL. News first (a
    press hit is the strongest update signal), then open web, then X. Each leg
    is independently best-effort so one dead source never blanks the others."""
    query = (f"{name} {company}".strip()
             + " new role OR raised OR launched OR announced OR joined OR award")
    candidates: list[dict] = []
    if _news_enabled():
        candidates += _google_news_search(f'"{name}" {company}'.strip())
    candidates += _pack_exa(_exa_search(query), "web")
    if _x_enabled():
        handle = _resolve_x_handle(db, contact)
        xq = (f"{name} {company}".strip()
              + (f" @{handle}" if handle else "")
              + " launch OR raised OR announcement OR milestone OR hiring")
        candidates += _pack_exa(
            _exa_search(xq, include_domains=["x.com"]), "x")
    seen_urls: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        u = (c.get("url") or "").strip()
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        unique.append(c)
    return unique[:12]


def find_updates(db, contact: models.Contact) -> list[dict]:
    """Find ONE recent public update about `contact` and emit it as an
    activity_update. Account-safe (news RSS + Exa web + Exa-scoped X; never the
    host's LinkedIn). Idempotent via contact.seen_post_ids keyed on the source
    URL. Returns the emitted change dicts ([] when nothing)."""
    name = (contact.name or "").strip()
    if not name:
        return []
    company = (contact.company or "").strip()
    company = "" if company.lower() in ("", "unknown") else company
    packed = _gather_candidates(db, contact, name, company)
    if not packed:
        return []
    user = (f"Contact: {name}" + (f", {company}" if company else "") + "\n"
            f"Recent web results:\n{json.dumps(packed, default=str)}")
    out = _llm_json(_EXTRACT_SYSTEM, user, max_tokens=300, cheap=True, background=True)
    if not out or not out.get("has_update") or not (out.get("summary") or "").strip():
        return []
    # Identity gate: web search collides on same-name different people. Drop any hit
    # we can't tie to THIS contact (company corroboration), so we never attribute a
    # stranger's news (e.g. a different "Vinita") to the wrong person.
    if not _identity_ok(out, packed, name, company):
        print(f"  [updates.web] dropped same-name mismatch for {name!r} "
              f"(matched_company={out.get('matched_company')!r}, "
              f"conf={out.get('identity_confidence')!r})", flush=True)
        return []
    url = (out.get("url") or "").strip()
    seen = _seen_urls(contact)
    if url and url in seen:
        return []
    kind = out.get("type") if out.get("type") in _KNOWN_KINDS else "new_post"
    # Attribute the update to the leg that surfaced the chosen result, so the
    # feed (and /_updates-status debugging) can tell news/web/x hits apart.
    via = next((p.get("via") for p in packed if (p.get("url") or "") == url),
               None) or "web"
    change = _emit(db, contact, kind, out["summary"][:300],
                   {"url": url, "headline": (out.get("headline") or "")[:120],
                    "source": via})
    if url:
        seen.add(url)
        contact.seen_post_ids = json.dumps(sorted(seen)[:200])
    return [change]


def run_updates(db, *, user_id: int | None = None, limit: int = 40) -> dict:
    """Scan up to `limit` contacts (optionally one user's) for recent public
    updates, emit activity_update rows, and commit once. Bounded so a run can't
    cost-spike Exa/Anthropic. Returns {scanned, emitted}."""
    q = db.query(models.Contact).filter(models.Contact.name.isnot(None))
    if user_id is not None:
        q = q.filter(models.Contact.user_id == user_id)
    contacts = q.limit(limit).all()
    scanned = emitted = 0
    for c in contacts:
        scanned += 1
        try:
            emitted += len(find_updates(db, c))
        except Exception as exc:  # noqa: BLE001 : one bad contact must not sink the run
            print(f"  [updates] contact={c.id} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    db.commit()
    print(f"[updates] scanned={scanned} emitted={emitted} "
          f"(user={user_id if user_id is not None else 'all'})", flush=True)
    return {"scanned": scanned, "emitted": emitted}
