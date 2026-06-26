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
    "press, or a notable post/talk. Ignore stale, generic, or unrelated results, "
    "and ignore results that are clearly a DIFFERENT person of the same name. "
    "Return ONLY JSON: {\"has_update\":true|false,\"type\":\"job_change|new_post|"
    "profile_update\",\"headline\":\"<=8 words\",\"summary\":\"<=25 words, "
    "specific, names the thing\",\"url\":\"<source url>\"}. Use job_change for a "
    "new role/company, new_post for a post/launch/press/talk, profile_update "
    "otherwise. has_update=false unless the evidence is solid and recent."
)


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
            json={"query": query, "numResults": n, "type": "auto",
                  "startPublishedDate": since,
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
