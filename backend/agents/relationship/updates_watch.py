"""agents/updates_watch.py : account-safe "what's new" via Exa web search.

relationship_watch.py emits the Today-feed `activity_update` rows by polling
LinkedIn through the host's Unipile account -- which risks the account and leaves
a "viewed your profile" footprint on every contact. This module emits the SAME
rows from PUBLIC web data (Exa neural search), so the Updates feed populates
WITHOUT ever touching LinkedIn or the host's account.

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
from datetime import datetime, timedelta, timezone

import httpx

from ... import models
from .. import exa
from .book import _llm_json          # shared Claude->JSON helper (+ tracing)
from .relationship_watch import _emit  # writes the activity_update row


# "Last month" window for a recent update. Env-tunable.
_LOOKBACK_DAYS = max(1, int(os.environ.get("UPDATES_LOOKBACK_DAYS", "30")))
_KNOWN_KINDS = {"job_change", "new_post", "profile_update"}

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
                n: int = 6) -> list[dict]:
    """Recent web results for `query` via Exa (newest-leaning, with text snippets).
    Returns [] when Exa isn't configured or on any failure -- best-effort only."""
    if not exa.exa_available():
        return []
    since = (datetime.now(timezone.utc)
             - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        r = httpx.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": exa._api_key(), "Content-Type": "application/json"},
            # No startPublishedDate / type filter: Exa drops results that lack a
            # publish date, and LinkedIn posts usually have none -- so those
            # params silently exclude the very updates we want. Recency is judged
            # by the LLM (_EXTRACT_SYSTEM) from the content instead.
            json={"query": query, "numResults": n,
                  "contents": {"text": {"maxCharacters": 600}}},
            timeout=20,
        )
        if r.status_code >= 300:
            return []
        return (r.json() or {}).get("results") or []
    except Exception:  # noqa: BLE001 : web lookup is best-effort
        return []


def _seen_urls(contact: models.Contact) -> set[str]:
    try:
        return set(json.loads(contact.seen_post_ids or "[]"))
    except Exception:  # noqa: BLE001
        return set()


def find_updates(db, contact: models.Contact) -> list[dict]:
    """Find ONE recent public update about `contact` and emit it as an
    activity_update. Account-safe (Exa only). Idempotent via contact.seen_post_ids
    keyed on the source URL. Returns the emitted change dicts ([] when nothing)."""
    name = (contact.name or "").strip()
    if not name:
        return []
    company = (contact.company or "").strip()
    company = "" if company.lower() in ("", "unknown") else company
    query = (f"{name} {company}".strip()
             + " new role OR raised OR launched OR announced OR joined OR award")
    results = _exa_search(query)
    if not results:
        return []
    packed = [{"title": x.get("title"), "url": x.get("url"),
               "published": x.get("publishedDate"),
               "text": (x.get("text") or "")[:500]} for x in results[:6]]
    user = (f"Contact: {name}" + (f", {company}" if company else "") + "\n"
            f"Recent web results:\n{json.dumps(packed, default=str)}")
    out = _llm_json(_EXTRACT_SYSTEM, user, max_tokens=300, cheap=True, background=True)
    if not out or not out.get("has_update") or not (out.get("summary") or "").strip():
        return []
    # Identity gate: web search collides on same-name different people. Drop any hit
    # we can't tie to THIS contact (company corroboration), so we never attribute a
    # stranger's news (e.g. a different "Vinita") to the wrong person.
    if not _identity_ok(out, packed, name, company):
        print(f"  [updates.exa] dropped same-name mismatch for {name!r} "
              f"(matched_company={out.get('matched_company')!r}, "
              f"conf={out.get('identity_confidence')!r})", flush=True)
        return []
    url = (out.get("url") or "").strip()
    seen = _seen_urls(contact)
    if url and url in seen:
        return []
    kind = out.get("type") if out.get("type") in _KNOWN_KINDS else "new_post"
    change = _emit(db, contact, kind, out["summary"][:300],
                   {"url": url, "headline": (out.get("headline") or "")[:120],
                    "source": "web"})
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
