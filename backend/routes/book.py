"""
routes/book.py : the whole relationship-side API surface (BookApp + spine).

Two routers in one module:

  router               (/api/book)          — the advisor "Your book today"
      surface: Today feed (Updates + Needs-outreach), draft/ask (+streams),
      the canonical relationship-detail endpoint, the run-updates sweep, and
      the admin diagnostics (`_diagnostics`, `_updates-test`, `_draft-preview`).
  relationships_router (/api/relationships) — the durable contact spine:
      contacts CRUD-ish, email threads/channel, import-conversations, chat
      (+stream), followup/schedule, snooze, star.

The feed is built from a DEMO BOOK (the advisor's roster) so the surface renders
end-to-end without a populated relationship spine; agents/book.py runs the real
LLM prompts over it when ANTHROPIC_API_KEY is set, and a deterministic heuristic
otherwise. Every spine route is owner-scoped (404-on-not-owned), so relationship
data never leaks across users. routes/relationships.py remains as a thin
re-export shim for importers of the old module path.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import billing_plans as bp
from .. import models
from ..agents.relationship import book as book_agent
from ..agents.relationship.spine import relationships as rel_agent
from ..agents.relationship.pipeline.context.network_search import enrich_book_ask
from ..auth import current_user, require_can_send_linkedin, require_paid
from ..db import SessionLocal, get_db
from ..integrations.unipile_config import unipile_creds

# Reuse the agent's stdout tracer so route- and agent-level [book] lines
# interleave in one Railway stream (grep `[book]`).
_trace = book_agent._btrace

# Relationship-type tags = the capture "This person is…" set. They drive the
# Book filter pills + search vocabulary. Legacy `recruiting` folds into hiring.
BOOK_TAGS = ["sales", "hiring", "investor", "partner", "follow_up"]


def _book_tags(contact_types) -> list[str]:
    out: list[str] = []
    for t in (contact_types or []):
        t = "hiring" if t == "recruiting" else t
        if t in BOOK_TAGS and t not in out:
            out.append(t)
    return out

router = APIRouter(prefix="/api/book", tags=["book"])


# ─── demo book : the advisor's roster ────────────────────────────────────────
# Fresh relative dates each call so "2h ago" / "Yesterday" stay accurate.

def _ago(*, hours: int = 0, days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours, days=days)).isoformat()


def _demo_book() -> list[dict]:
    return [
        # ── people with a noteworthy update (recently active → not "overdue") ──
        {
            "id": "james-holloway", "name": "James Holloway", "vip": True,
            "title": "General counsel", "firm": "Meridian Capital", "tier": "key",
            "days_since": 12, "cadence_days": 60, "review_due": False,
            "met_at": "NYC Tech Week", "value": "$60M relationship",
            "interaction_history": "Sold his logistics company in 2021; you "
                "manage the proceeds. Last spoke at his daughter's graduation.",
            "raw_signals": {"type": "liquidity_event",
                            "headline": "Liquidity event flagged",
                            "detected_at": _ago(hours=2),
                            "significance": "high", "outreach_trigger": True},
        },
        {
            "id": "priya-nadel", "name": "Priya Nadel", "vip": False,
            "title": "Principal", "firm": "Lumen Growth", "tier": "a",
            "days_since": 20, "cadence_days": 90, "review_due": False,
            "met_at": "Milken", "value": "$12M relationship",
            "interaction_history": "Met through the Whartonalumni network; "
                "you handle her family trust.",
            "raw_signals": {"type": "promotion",
                            "headline": "Promoted to MD, Lumen Growth",
                            "detected_at": _ago(days=1),
                            "significance": "medium", "outreach_trigger": True},
        },
        {
            "id": "david-osei", "name": "David Osei", "vip": True,
            "title": "Partner", "firm": "Crestline Partners", "tier": "key",
            "days_since": 5, "cadence_days": 90, "review_due": False,
            "met_at": "NYC Tech Week", "value": "$35M relationship",
            "interaction_history": "Long-time client; you structured his "
                "carry. Talks about his kids' college planning often.",
            "raw_signals": {"type": "fundraise",
                            "headline": "Raised a new fund",
                            "detected_at": _ago(days=3),
                            "significance": "high", "outreach_trigger": True},
        },
        # ── people overdue for a touch (the "Needs outreach" list) ──
        {"id": "thomas-reyes", "name": "Thomas Reyes", "vip": False,
         "title": "SVP finance", "firm": "Atlas Pension", "tier": "core",
         "days_since": 64, "cadence_days": 45, "review_due": False,
         "met_at": "SALT",
         "interaction_history": "Estate planning client. Last talked about a "
            "second home in Tahoe."},
        {"id": "margaret-chen", "name": "Margaret Chen", "vip": True,
         "title": "Founder", "firm": "Chen Family Office", "tier": "key",
         "days_since": 18, "cadence_days": 60, "review_due": True,
         "met_at": "SALT", "value": "$40M relationship",
         "interaction_history": "Annual portfolio review is due this month. "
            "Risk-averse; values a clear agenda."},
        {"id": "naomi-vance", "name": "Naomi Vance", "vip": False,
         "title": "Partner", "firm": "Vance Family Office", "tier": "a",
         "days_since": 41, "cadence_days": 35, "review_due": True,
         "met_at": "Milken",
         "interaction_history": "Review overdue. Co-invests with two of your "
            "other clients."},
        {"id": "sofia-klein", "name": "Sofia Klein", "vip": False,
         "title": "Managing director", "firm": "Klein Advisory", "tier": "a",
         "days_since": 38, "cadence_days": 30, "review_due": False,
         "met_at": "SALT",
         "interaction_history": "Referred three clients last year. Loves "
            "sailing; usually off-grid in August."},
        {"id": "raj-patel", "name": "Raj Patel", "vip": False,
         "title": "VP finance", "firm": "Northwind", "tier": "core",
         "days_since": 52, "cadence_days": 40, "review_due": False,
         "met_at": "NYC Tech Week",
         "interaction_history": "Rolling over a 401k; awaiting paperwork."},
        {"id": "elena-fischer", "name": "Elena Fischer", "vip": False,
         "title": "Owner", "firm": "Fischer Group", "tier": "a",
         "days_since": 71, "cadence_days": 45, "review_due": False,
         "met_at": "Milken",
         "interaction_history": "Business-sale conversation stalled last spring."},
        {"id": "marcus-webb", "name": "Marcus Webb", "vip": False,
         "title": "Director", "firm": "Webb & Associates", "tier": "core",
         "days_since": 29, "cadence_days": 30, "review_due": True,
         "met_at": "NYC Tech Week",
         "interaction_history": "Mid-year check-in due; new baby last year."},
        {"id": "grace-lin", "name": "Grace Lin", "vip": False,
         "title": "Partner", "firm": "Lin Wealth", "tier": "core",
         "days_since": 45, "cadence_days": 35, "review_due": False,
         "met_at": "SALT",
         "interaction_history": "Tax-loss harvesting question still open."},
        {"id": "daniel-okafor", "name": "Daniel Okafor", "vip": False,
         "title": "Executive", "firm": "Okafor Holdings", "tier": "a",
         "days_since": 90, "cadence_days": 45, "review_due": False,
         "met_at": "Milken",
         "interaction_history": "Went quiet after a market dip; reassurance call "
            "never happened."},
        {"id": "hannah-brooks", "name": "Hannah Brooks", "vip": False,
         "title": "Founder", "firm": "Brooks Studio", "tier": "core",
         "days_since": 33, "cadence_days": 30, "review_due": False,
         "met_at": "NYC Tech Week",
         "interaction_history": "Just started a college fund for her twins."},
        # ── fresh captures (the "Prospects" filter / "New" health) ──
        {"id": "elena-marsh", "name": "Elena Marsh", "vip": False,
         "title": "Principal", "firm": "Hawthorn Wealth", "tier": "core",
         "days_since": 0, "cadence_days": 45, "review_due": False,
         "met_at": "NYC Tech Week", "is_prospect": True,
         "interaction_history": "Just met, exchanged badges at the afterparty."},
    ]


def _real(val: Optional[str]) -> str:
    """Strip the 'Unknown' schema placeholder; treat it as empty."""
    s = (val or "").strip()
    return "" if s.lower() == "unknown" else s


def _book_from_spine(db: Session, user: models.User) -> list[dict]:
    """Map the real Contact spine into the book shape. Empty when the user has
    no contacts — caller falls back to the demo book."""
    t = time.monotonic()
    contacts = rel_agent.list_contacts(db, user.id)
    t_list = time.monotonic() - t
    if not contacts:
        return []
    t = time.monotonic()
    inter_index = rel_agent.prefetch_interactions_by_prospect(db, contacts)
    t_inter = time.monotonic() - t
    t = time.monotonic()
    update_index = rel_agent.prefetch_activity_updates_by_contact(db, contacts)
    # Contact-keyed interaction index so last_touch works for import-path
    # contacts (no Prospect); their history hangs off contact_id.
    contact_index = rel_agent.prefetch_interactions_by_contact(db, contacts)
    t_upd = time.monotonic() - t
    rel_agent._spine_prof_reset()
    t = time.monotonic()
    out = _book_from_spine_contacts(db, user, contacts, inter_index, update_index,
                                    contact_index)
    t_loop = time.monotonic() - t
    prof = rel_agent.spine_prof()
    _trace(f"_book_from_spine {len(contacts)} contacts: list={t_list:.2f}s "
           f"prefetch_inter={t_inter:.2f}s prefetch_upd={t_upd:.2f}s "
           f"summary_loop={t_loop:.2f}s "
           f"(prospects={prof['prospects']:.2f}s events={prof['events']:.2f}s "
           f"timeline={prof['timeline']:.2f}s identity={prof['identity']:.2f}s)")
    return out


def _find_contact_orm(db: Session, user: models.User, contact_id: Optional[str]):
    """Resolve the durable Contact ORM row for a numeric book id, so the
    consolidated drafter can pull this person's real thread + the host's voice.
    None when the id isn't a plain int (the demo book uses slugs) or no match."""
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return None
    return next((c for c in rel_agent.list_contacts(db, user.id) if c.id == cid),
                None)


def _find_contact_fast(db: Session, user: models.User,
                       contact_id: str) -> Optional[dict]:
    """Single-contact lookup by numeric DB id — skips rebuilding the full book.
    Returns None when contact_id isn't a plain integer (demo book uses slugs)."""
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return None
    contacts = rel_agent.list_contacts(db, user.id)
    match = next((c for c in contacts if c.id == cid), None)
    if not match:
        return None
    inter_index = rel_agent.prefetch_interactions_by_prospect(db, [match])
    update_index = rel_agent.prefetch_activity_updates_by_contact(db, [match])
    contact_index = rel_agent.prefetch_interactions_by_contact(db, [match])
    book = _book_from_spine_contacts(db, user, [match], inter_index, update_index,
                                     contact_index)
    return book[0] if book else None


def _book_from_spine_contacts(db, user, contacts, inter_index, update_index,
                              contact_index=None):
    """Inner loop of _book_from_spine, reusable for single-contact fast path."""
    now = datetime.now(timezone.utc)
    book = []
    for c in contacts:
        # `or []`, NOT `.get(c.id)`: a contact with no updates isn't in the index,
        # so .get returns None -> contact_summary reads that as "not prefetched"
        # and fires a per-contact fetch_activity_updates DB query (the N+1 that
        # made summary_loop ~10s for 80 contacts). [] = "prefetched, none".
        row = rel_agent.contact_summary(
            db, c, inter_index, update_index.get(c.id) or [],
            interactions_by_contact=(contact_index if contact_index is not None else {}))
        # days_since = None means "no known interaction" (never messaged / no
        # synced history) -- distinct from 0 ("touched today"), so the UI can say
        # so honestly instead of a misleading "moments ago".
        days = None
        last = row.get("last_touch_at")
        if last is not None:
            try:
                dt = last if isinstance(last, datetime) else datetime.fromisoformat(str(last))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = max(0, (now - dt).days)
            except Exception:
                days = None
        upd = row.get("latest_update") or {}
        # Prefer a specific title, but a bare "Update" (account-cooling and other
        # kinds not in _TITLES) tells the user nothing -- fall back to the summary
        # ("Casus Capital is cooling: no touch in 34 days"), which is what makes
        # the feed read as real intelligence rather than a wall of "Update".
        _ttl = (upd.get("title") or "").strip()
        headline = (_ttl if _ttl and _ttl.lower() != "update"
                    else (upd.get("summary") or _ttl or "Update"))
        signals = None
        if headline:
            occurred = upd.get("occurred_at")
            signals = {
                "type": upd.get("type") or "company_news",
                "headline": headline,
                "detected_at": (occurred.isoformat() if isinstance(occurred, datetime)
                                else (occurred or _ago())),
                "significance": "medium",
                "outreach_trigger": True,
                # The pre-written follow-up (only present on IMPORTANT updates --
                # job changes / milestones). Rides through to the Today feed so the
                # draft is already there, no on-tap compose.
                "draft": upd.get("draft"),
                "draft_subject": upd.get("draft_subject"),
            }
        identity = row.get("identity") or {}
        book.append({
            "id": str(row.get("contact_id")),
            "name": _real(row.get("name")) or "Unknown",
            "vip": bool(getattr(c, "vip", False)),
            "title": _real(identity.get("headline")) or _real(identity.get("role")),
            "firm": _real(row.get("company")) or _real(identity.get("company")),
            "tier": "core",
            "days_since": days,
            "cadence_days": 30,
            "review_due": False,
            "met_at": row.get("met_at") or "",
            "value": "",
            "is_prospect": not row.get("is_connection"),
            # Lets the UI label a no-history row honestly ("Connected on
            # LinkedIn") instead of a misleading time when days_since is null.
            "has_linkedin": bool(row.get("linkedin_url")),
            "stage": row.get("relationship_stage"),
            "interaction_history": row.get("next_step") or "",
            "raw_signals": signals,
            # Relationship-type tags (sales/hiring/investor/partner/follow_up)
            # for the Book filter pills + search.
            "tags": _book_tags(row.get("contact_types")),
        })
    return book


# Per-user built-book cache. Building the book is O(all contacts) -- once the
# book went contact-first (494 contacts here vs ~5 prospects before), every ask
# AND every Today render rebuilt all of it. We cache the built book keyed by a
# CHEAP fingerprint (contact count + newest interaction timestamp), so repeated
# reads reuse one build, but the instant anything changes -- a capture adds a
# contact, an update lands a new interaction -- the fingerprint moves and the
# next read rebuilds. This keeps the feed fast WITHOUT ever showing a stale book
# (no fixed TTL freshness gap for the demo's capture-then-Today moment). The TTL
# is only a backstop for changes the fingerprint can't see (e.g. a bare field
# edit). Per worker process; each computes its own fingerprint, so it is
# correct across Railway's multiple workers with no cross-process signalling.
_BOOK_CACHE: dict = {}                       # user_id -> (fingerprint, book, built_at)
_BOOK_CACHE_LOCK = threading.Lock()
_BOOK_CACHE_TTL = float(os.environ.get("BOOK_CACHE_TTL", "300"))


def _book_fingerprint(db: Session, user_id: int) -> tuple:
    """A cheap key that moves when the roster changes: the user's contact count.
    Deliberately does NOT fold in the newest interaction timestamp -- the daily
    Bright Data sweep adds interactions to existing contacts constantly, which
    would move the key on every request and defeat the cache entirely. A capture
    (the moment freshness matters for the demo) adds a NEW contact, so the count
    moves and the cache invalidates; passive sweep updates to existing people
    ride the TTL instead. One tiny indexed COUNT -- far cheaper than a build. On
    error returns a unique sentinel so we simply don't serve from cache."""
    from sqlalchemy import func
    try:
        n = (db.query(func.count(models.Contact.id))
               .filter(models.Contact.user_id == user_id).scalar()) or 0
        return (n,)
    except Exception:  # noqa: BLE001 : never let the key computation break a load
        return (object(),)


def _load_book(db: Session, user: models.User) -> list[dict]:
    """Real book from the spine; only DEMO users fall back to the demo roster
    (a real account with an empty spine gets an empty book, not fake clients).

    Served from a per-user fingerprint cache so a large contact-first book does
    not get rebuilt on every ask/Today render."""
    t0 = time.monotonic()
    fp = _book_fingerprint(db, user.id)
    now = time.monotonic()
    with _BOOK_CACHE_LOCK:
        hit = _BOOK_CACHE.get(user.id)
    if hit and hit[0] == fp and (now - hit[2]) < _BOOK_CACHE_TTL:
        _trace(f"load_book user={user.id} -> {len(hit[1])} contacts (cache) "
               f"in {time.monotonic()-t0:.2f}s")
        return hit[1]

    book = _book_from_spine(db, user)
    from ..auth import is_demo_user
    if is_demo_user(user):
        # In the demo, a real capture must ADD to the seeded roster, not replace
        # it: just-scanned people show first, the 14 demo contacts stay. (Without
        # this, the first scan makes the spine non-empty and the demo book vanishes.)
        book = book + _demo_book()
        src = "demo+spine"
    elif book:
        src = "spine"
    else:
        book = []
        src = "empty"
    with _BOOK_CACHE_LOCK:
        _BOOK_CACHE[user.id] = (fp, book, now)
    _trace(f"load_book user={user.id} -> {len(book)} contacts ({src}) "
           f"in {time.monotonic()-t0:.2f}s")
    return book


def _host_name(user: models.User) -> str:
    return (getattr(user, "name", None) or "").strip() or "the host"


def _find_contact(book: list[dict], *, contact_id: Optional[str],
                  name: Optional[str]) -> Optional[dict]:
    for c in book:
        if contact_id and c.get("id") == contact_id:
            return c
        if name and (c.get("name") or "").lower() == name.lower():
            return c
    return None


# ─── request bodies ──────────────────────────────────────────────────────────

class DraftIn(BaseModel):
    contact_id: Optional[str] = None
    name: Optional[str] = None
    trigger: str                       # "Promoted to MD" | "Quiet 38 days, review due"
    channel: str = "email"             # email | linkedin | sms


class AskIn(BaseModel):
    query: str
    # Which tab the ask came from: "book" (Today/updates) or "referral"
    # (network/intros). None = legacy caller (no routing). The natural intent is
    # detected server-side regardless; `mode` only decides whether we tell the
    # client to SWITCH tabs (via `routed_to`), so a referral ask typed in Today
    # is answered as a referral and the UI can follow.
    mode: Optional[str] = None


# Strong REFERRAL intent the shared detect_network_intent (which keys on
# "who do I know at / 2nd degree / intro / refer") misses: the equally-common
# "reach someone new through my network" verbs. Kept HERE (router-local), not in
# detect_network_intent, so the existing network-search gate + prod ask flow are
# byte-for-byte unchanged -- this only widens the tab-routing signal. Precise on
# purpose: "reach out" (a book verb) is explicitly excluded so it can't leak.
_STRONG_REFERRAL_RE = re.compile(
    r"\bconnect\s+(?:me\s+)?(?:with|to)\b"
    r"|\bput\s+me\s+in\s+touch\b"
    r"|\b(?:get|put)\s+(?:me\s+)?in\s+front\s+of\b"
    r"|\bget\s+me\s+an?\s+intro"
    r"|\bintroduce\s+me\b"
    r"|\b(?:want|need|hoping|love|'?d\s+like)\s+to\s+meet\b"
    r"|\bmeet\s+(?:the\s+)?(?:founder|founders|team|ceo|cto|cfo|partner|partners|people)\b"
    r"|\breach\b(?!\s+out)\s+(?:the\s+)?\w",
    re.I,
)

# Strong BOOK intent: explicit existing-relationship verbs. Used only to tell a
# confident "book" ask apart from a genuinely AMBIGUOUS one -- so ambiguous asks
# stay put instead of being force-labeled book (the old binary bug).
_STRONG_BOOK_RE = re.compile(
    r"\breach\s+out\s+to\b"
    r"|\breconnect\b"
    r"|\bfollow[-\s]?up\s+with\b"
    r"|\blast\s+(?:talked|spoke|messaged|met|contact|reached)\b"
    r"|\bgone\s+quiet\b"
    r"|\bwhat'?s\s+new\b"
    r"|\bcongratulate\b"
    r"|\bdraft\s+(?:a\s+)?(?:note|message|reply|dm|email)\b",
    re.I,
)


def _ask_signal(query: str) -> str:
    """Three-state intent: 'referral' | 'book' | 'ambiguous'.

    referral  = the shared network-intent gate OR a strong reach-through verb
    book      = an explicit existing-relationship verb
    ambiguous = a target (company/topic/status) with NO direction verb, e.g.
                'people at Stripe' -- resolvable only by which tab you're in.

    Deterministic, no LLM. detect_network_intent is checked (never mutated), so
    routing and the existing enrichment gate never disagree on the referral set.
    """
    from ..agents.relationship.pipeline.context.network_search import (
        detect_network_intent)
    q = query or ""
    if detect_network_intent(q) or _STRONG_REFERRAL_RE.search(q):
        return "referral"
    if _STRONG_BOOK_RE.search(q):
        return "book"
    return "ambiguous"


def _natural_ask_mode(query: str) -> str:
    """Binary view of _ask_signal for the enrichment/answer path (ambiguous folds
    to 'book', its safe default). Legacy shape; routing uses _route_response."""
    return "referral" if _ask_signal(query) == "referral" else "book"


def _routed_to(requested: Optional[str], natural: str) -> Optional[str]:
    """The tab to SWITCH to, or None to stay. Fires only on a confident mismatch
    (the caller declared a tab and a STRONG signal points elsewhere). Ambiguous
    never reaches here -- see _route_response."""
    req = (requested or "").strip().lower()
    if req in ("book", "referral") and req != natural:
        return natural
    return None


def _route_response(requested: Optional[str], query: str) -> tuple:
    """Full routing decision for a tab-aware ask. Returns (routed_to, cross_hint):

      routed_to  -- switch the client to this tab NOW (confident opposite signal)
      cross_hint -- stay put, but softly offer this other tab (ambiguous ask)

    Legacy callers (no declared tab) get (None, None) -- behavior unchanged.
    """
    req = (requested or "").strip().lower()
    if req not in ("book", "referral"):
        return None, None
    sig = _ask_signal(query)
    if sig == "ambiguous":
        # Trust the tab the user is standing in; nudge, never yank.
        return None, ("referral" if req == "book" else "book")
    if sig != req:
        return sig, None            # confident mismatch -> auto-switch
    return None, None               # confident match -> stay


# ─── routes ──────────────────────────────────────────────────────────────────

@router.get("/today")
def today(db: Session = Depends(get_db),
          user: models.User = Depends(current_user)):
    """The cached-shape Today feed : time-ordered Updates + priority-ranked
    Needs-outreach. Built by running detection + scoring across the book."""
    t0 = time.monotonic()
    book = _load_book(db, user)
    feed = book_agent.build_today(book)
    name = _host_name(user)
    feed["advisor_name"] = name
    # Warm drafts in the background ONLY for the top few the user is likely to
    # open first, so the draft panel is usually instant on tap. Pre-drafting the
    # WHOLE feed (hundreds of updates) fired hundreds of 8-22s LLM calls that
    # saturated the pool and starved the foreground ask (a 'who's gone quiet' was
    # seen taking 76s behind them). The rest draft lazily on tap -- the draft
    # sheet already composes on open when the cache is cold. The feed is already
    # ordered (updates newest-first, needs-outreach by priority), so the head is
    # the most-likely-opened slice.
    predraft_max = max(0, int(os.environ.get("TODAY_PREDRAFT_MAX", "12")))
    by_id = {c.get("id"): c for c in book}
    pairs = [(by_id[r["contact_id"]], r.get("trigger") or "catching up")
             for r in feed["needs_outreach"] + feed["updates"]
             if r.get("contact_id") in by_id and (r.get("can_draft") is not False)]
    pairs = pairs[:predraft_max]
    book_agent.predraft(pairs, user_name=name)
    _trace(f"GET /today user={user.id}: {len(feed['updates'])} updates, "
           f"{len(feed['needs_outreach'])} needs-outreach, predraft {len(pairs)} "
           f"(cap {predraft_max}) in {time.monotonic()-t0:.2f}s")
    return feed



@router.post("/draft")
def draft(body: DraftIn, db: Session = Depends(get_db),
          user: models.User = Depends(current_user)):
    """The note behind a 'Draft' tap : warm congratulation or cold re-engage,
    chosen from the trigger."""
    book = _load_book(db, user)
    contact = _find_contact(book, contact_id=body.contact_id, name=body.name)
    if contact is None:
        # Still draftable from just a name + trigger (the agent can work with
        # the trigger alone), so synthesize a minimal contact rather than 404.
        contact = {"name": body.name or "there", "title": "", "firm": "",
                   "interaction_history": ""}
    name = _host_name(user)
    t0 = time.monotonic()
    # Consolidated path: when this maps to a real Contact, draft through the ONE
    # shared composer (voice + real prior-message thread + no em dashes). Falls
    # back to the book heuristic drafter for demo-book slugs or on any miss.
    msg = None
    engine = "shared"
    contact_orm = _find_contact_orm(db, user, body.contact_id)
    if contact_orm is not None:
        from ..agents.relationship.pipeline.compose import drafting
        msg = drafting.compose_followup(
            db, user.id, contact_orm, reason=body.trigger, channel=body.channel)
    if msg is None:
        engine = "heuristic"
        msg = book_agent.draft_message_cached(
            contact, body.trigger, channel=body.channel,
            user_name=name)
    _trace(f"POST /draft user={user.id} to={contact.get('name')!r} "
           f"channel={body.channel} trigger={body.trigger!r} engine={engine} "
           f"in {time.monotonic()-t0:.2f}s")
    return {"channel": body.channel, **msg}


@router.post("/draft/stream")
def draft_stream(body: DraftIn, db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)):
    """Token-by-token streamed draft (live 'typing', like Claude). Real contacts
    stream through the shared composer (voice + real thread); demo-book slugs fall
    back to the heuristic emitted as one chunk. Bytes flow immediately and never
    stop until done, so the edge timeout (524) can't fire.

    Events: token {t} (append to the draft) · done {total_s} · error {detail}.
    """
    user_id = user.id
    cid, nm = body.contact_id, body.name
    trigger, channel = body.trigger, body.channel
    name = _host_name(user)

    def gen():
        yield ": open\n\n"  # flush headers immediately
        from ..db import SessionLocal
        wdb = SessionLocal()
        t0 = time.monotonic()
        streamed = False
        try:
            wuser = wdb.query(models.User).get(user_id)
            orm = _find_contact_orm(wdb, wuser, cid)
            if orm is not None:
                from ..agents.relationship.pipeline.compose import drafting
                for chunk in drafting.compose_stream(wdb, user_id, orm,
                                                     reason=trigger, channel=channel):
                    streamed = True
                    yield f"event: token\ndata: {json.dumps({'t': chunk})}\n\n"
            if not streamed:
                # No real contact (demo slug) or no key: emit the heuristic body
                # as a single chunk so the UI still gets a draft.
                book = _load_book(wdb, wuser)
                contact = _find_contact(book, contact_id=cid, name=nm) or \
                    {"name": nm or "there", "title": "", "firm": "",
                     "interaction_history": ""}
                msg = book_agent.draft_message_cached(
                    contact, trigger, channel=channel, user_name=name)
                yield f"event: token\ndata: {json.dumps({'t': msg.get('body') or ''})}\n\n"
            yield f"event: done\ndata: {json.dumps({'total_s': round(time.monotonic()-t0, 1)})}\n\n"
            _trace(f"POST /draft/stream user={user_id} to={nm!r} "
                   f"in {time.monotonic()-t0:.1f}s (streamed={streamed})")
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'detail': f'{type(exc).__name__}: {exc}'})}\n\n"
            _trace(f"POST /draft/stream user={user_id} FAILED: {type(exc).__name__}: {exc}")
        finally:
            wdb.close()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@router.post("/ask/stream")
def ask_stream(body: AskIn, db: Session = Depends(get_db),
               user: models.User = Depends(current_user)):
    """Streaming `/ask` (Server-Sent Events). Same work as /ask, but emits the
    ranked people the instant selection finishes, then each drafted card as it
    completes, with a heartbeat so the connection is NEVER silent. Because bytes
    start flowing immediately and keep flowing, Cloudflare's 100s read timeout
    (the 524 'server took too long') can't fire -- a slow moment degrades to
    'still drafting…' instead of a hard error.

    Events: status {phase[,name]} · people {people,answer} · person {index,
    contact_id,name,draft} · done {total_s,count} · error {detail}.
    """
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(422, "query is required")
    user_id = user.id
    events: "queue.Queue" = queue.Queue()

    def work():
        from ..db import SessionLocal
        from ..agents.relationship.pipeline.compose import drafting
        from concurrent.futures import ThreadPoolExecutor, as_completed
        wdb = SessionLocal()
        t0 = time.monotonic()
        try:
            wuser = wdb.query(models.User).get(user_id)
            events.put(("status", {"phase": "selecting"}))
            book = _load_book(wdb, wuser)
            contacts_orm = rel_agent.list_contacts(wdb, user_id)
            res = book_agent.ask_agent(book, q)          # selection (Haiku, gated)
            res = enrich_book_ask(wuser, q, contacts_orm, res,
                                  force=(body.mode == "referral"))
            people = res.get("people") or []
            network_hits = res.get("network_hits") or []
            orm_by_id = {str(c.id): c for c in contacts_orm}
            by_name = {(c.get("name") or "").strip().lower(): c for c in book}
            for p in people:
                bd = by_name.get((p.get("name") or "").strip().lower())
                if bd and bd.get("id"):
                    p["contact_id"] = bd["id"]
            # Show the ranked list NOW (drafts fill in next) -- first paint ~3s.
            # `routed_to` rides the first paint so the UI can switch tabs the
            # instant results are ready, not after all drafts land.
            _routed, _hint = _route_response(body.mode, q)
            events.put(("people", {
                "people": people,
                "answer": res.get("answer"),
                "network_hits": network_hits,
                "routed_to": _routed,
                "cross_hint": _hint,
            }))
            # Draft the top few, emitting each as it lands. Build DB contexts
            # SERIALLY (session not thread-safe), then fan the pure-LLM calls out.
            inline = max(0, int(os.environ.get("ASK_INLINE_DRAFTS", "6")))
            name_ = _host_name(wuser)
            targets = []        # real ORM contacts -> token-stream via shared composer
            heuristic = []      # demo-book / no-ORM people -> one-shot agent draft
            for idx, p in enumerate(people[:inline]):
                bd = by_name.get((p.get("name") or "").strip().lower())
                orm = orm_by_id.get(str(bd.get("id"))) if bd else None
                if orm is not None:
                    targets.append((idx, p, drafting.build_context(wdb, user_id, orm)))
                elif bd is not None:
                    heuristic.append((idx, p, bd))

            # Real contacts type out token-by-token through the shared composer
            # (voice + real prior thread). Demo-book / no-thread people are drafted
            # by the book agent (one LLM call) and emitted as a single chunk so
            # their card still fills in -- otherwise the demo shows reasons with no
            # drafted message (the "agent isn't drafting" bug).
            def _stream_one(idx, p, ctx):
                events.put(("status", {"phase": "drafting", "name": p.get("name")}))
                for delta in drafting.stream_from_context(
                        ctx, p.get("reason") or "following up", "email",
                        directive=q):
                    events.put(("token", {"index": idx, "t": delta}))
                events.put(("person", {"index": idx, "contact_id": p.get("contact_id"),
                                       "name": p.get("name")}))

            def _heuristic_one(idx, p, bd):
                events.put(("status", {"phase": "drafting", "name": p.get("name")}))
                try:
                    reason_ = p.get("reason") or "following up"
                    # Fold the host's ask-bar instruction into the trigger so the
                    # demo / no-thread path honors it too (and caches per-trigger).
                    trig_ = f"{reason_}. Host's instruction: {q}" if q else reason_
                    msg = book_agent.draft_message_cached(
                        bd, trig_, channel="email",
                        user_name=name_)
                    body_ = (msg or {}).get("body") or ""
                    if body_:
                        events.put(("token", {"index": idx, "t": body_}))
                except Exception:  # noqa: BLE001
                    pass
                events.put(("person", {"index": idx, "contact_id": p.get("contact_id"),
                                       "name": p.get("name")}))

            if targets or heuristic:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = [ex.submit(_stream_one, idx, p, ctx) for idx, p, ctx in targets]
                    futs += [ex.submit(_heuristic_one, idx, p, bd) for idx, p, bd in heuristic]
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception:  # noqa: BLE001 : one bad draft must not sink the stream
                            pass
            events.put(("done", {"total_s": round(time.monotonic() - t0, 1),
                                 "count": len(people),
                                 "network_hits": network_hits}))
            _trace(f"POST /ask/stream user={user_id} q={q!r} -> {len(people)} people "
                   f"in {time.monotonic()-t0:.1f}s (streamed)")
        except Exception as exc:  # noqa: BLE001
            events.put(("error", {"detail": f"{type(exc).__name__}: {exc}"}))
            _trace(f"POST /ask/stream user={user_id} FAILED: {type(exc).__name__}: {exc}")
        finally:
            wdb.close()
            events.put(None)  # sentinel: end of stream

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield ": open\n\n"  # flush headers immediately -> CF read-timeout satisfied
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"  # never silent > 15s, so CF can't 524
                continue
            if item is None:
                break
            event, data = item
            yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/relationship/{contact_id}")
def relationship(contact_id: str, db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)):
    """THE canonical relationship-detail endpoint (absorbed the old
    GET /api/relationships/contacts/{id}).

    The BookApp view: health, the plain-language 'why', the relationship value,
    and a synthesized timeline. When `contact_id` is a real owned spine Contact
    (numeric DB id, not a demo-book slug), the durable-person payload the old
    contacts route served rides along too:

      contact_summary — the rollup summary (name/company/stage/n_events/...)
      events          — the per-event breakdown ('events we've shared')
      spine_timeline  — the unified cross-event interaction timeline
                        (named apart from the book-view `timeline`)

    Owner-scoped like the route it replaced: someone else's contact id 404s
    exactly like a missing one. The drafted message is fetched separately via
    /draft so it can be refined independently."""
    t0 = time.monotonic()
    # Try to look up the contact directly by DB id first (avoids rebuilding the
    # entire book just to find one person).
    fast = _find_contact_fast(db, user, contact_id)
    contact = fast or _find_contact(_load_book(db, user), contact_id=contact_id, name=None)
    if contact is None:
        _trace(f"GET /relationship/{contact_id} user={user.id}: NOT FOUND "
               f"in {time.monotonic()-t0:.2f}s")
        raise HTTPException(404, "contact not found")
    detail = book_agent.relationship_detail(contact)
    # Spine extras for a durable Contact (demo-book slugs have no spine row).
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        cid = None
    if cid is not None:
        c = db.get(models.Contact, cid)
        if c is not None and getattr(c, "user_id", None) == user.id:
            detail["contact_summary"] = rel_agent.contact_summary(db, c)
            detail["events"] = rel_agent.contact_events(db, c)
            detail["spine_timeline"] = rel_agent.contact_timeline(db, c)
    _trace(f"GET /relationship/{contact_id} user={user.id} "
           f"({'fast' if fast else 'full-book'}) in {time.monotonic()-t0:.2f}s")
    return detail


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """Constant-time compare X-Admin-Token against ADMIN_TOKEN env (same gate as
    /admin/run-followups). Lets the scheduled GitHub Action fire the updates run
    without a user session."""
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="forbidden")


def _run_updates_sweep(db, user_id: Optional[int], limit: int) -> None:
    """Detached "what's new" sweep body (run via jobs.run_detached). Top-level so
    it stays importable; `db` is the run_detached-owned session. user_id=None
    sweeps all users."""
    from ..agents.relationship.updates_engine import run_sweep
    res = run_sweep(db, user_id=user_id, limit=limit)
    print(f"[updates] sweep {res}", flush=True)


@router.post("/run-updates", status_code=202)
def run_updates_endpoint(user_id: Optional[int] = None, limit: int = 40,
                         _: None = Depends(_require_admin_token)):
    """Scheduled "what's new" sweep -> activity_update rows the Today feed reads.

    Resilient engine: Bright Data (scrapes profile job-changes + milestone posts
    on its own infra, delivered via /webhooks/brightdata) when configured, else
    account-safe Exa web search. Tiered by the vip ⭐ flag so paid scraping spend
    tracks the contacts that matter. `limit` caps contacts per run — pass a small
    value (e.g. ?limit=2) for a cheap validation batch. Runs detached with its
    own session so the request returns immediately."""
    from ..jobs import run_detached
    run_detached(_run_updates_sweep, user_id, max(1, min(limit, 200)))
    return {"status": "started", "scope": user_id if user_id is not None else "all",
            "limit": max(1, min(limit, 200))}


@router.get("/_draft-preview")
def draft_preview_endpoint(user_id: int, limit: int = 6,
                           db: Session = Depends(get_db),
                           _: None = Depends(_require_admin_token)):
    """Messaging-quality inspector: for `user_id`, run the real composer over their
    top `limit` contacts and return, per contact, the draft + WHY it was written
    that way (the deterministic 'natural move' + the facts/voice/register the
    composer saw). Read-only (compose only, never sends). Bounded (limit<=20) and
    on-demand, so no recurring load. The fast way to answer 'is the agent honing
    in / sounding like them?' across a real book, and to spot who has no voice yet.

        curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \\
             "https://event.surpluslayer.com/api/book/_draft-preview?user_id=171&limit=6" | jq
    """
    import concurrent.futures
    from ..agents.relationship.pipeline.compose import drafting
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    n = max(1, min(limit, 20))
    contacts = rel_agent.list_contacts(db, user_id)[:n]
    voice_block = drafting._voice_block_for(db, user_id, "linkedin")
    # DB reads serial (session not thread-safe); the LLM composes fan out.
    ctxs = [drafting.build_context(db, user_id, c, voice_block, channel="linkedin")
            for c in contacts]
    # ALL store facts per contact (incl. META like channel_preference, which the
    # draft grounding excludes) -- gathered serially here, read in the fan-out.
    from ..agents.relationship.spine import memory as _cm
    store_all = [_cm.get_facts(db, c.id) for c in contacts]

    def _row(i: int) -> dict:
        ctx = ctxs[i]
        facts = ctx.get("facts") or {}
        reason = (facts.get("latest_update") or facts.get("next_step")
                  or "reconnecting")
        d = drafting.compose_from_context(ctx, reason, "linkedin")
        return {
            "name": ctx.get("name"),
            "natural_move": drafting._natural_action(ctx) or "(reconnect / general)",
            "reason_used": reason,
            "has_voice": bool((ctx.get("voice_block") or "").strip()),
            "register": ctx.get("register"),
            "facts": {k: facts.get(k) for k in
                      ("met_at", "next_step", "latest_update", "stage",
                       "relationship_types")},
            # EVERY knowledge-store fact for this contact, tagged with source +
            # age + mode -- so we can SEE what's stored and what reached the draft.
            # `grounded` = surfaced into the draft; META facts (channel_preference)
            # are stored + visible but not grounded.
            "store_facts": [
                {"key": f.key, "value": f.value, "source": f.source,
                 "confidence": f.confidence,
                 "grounded": f.key in {p["key"] for p in (ctx.get("store_provenance") or [])},
                 "observed_at": (f.observed_at.isoformat()
                                 if hasattr(f.observed_at, "isoformat") else None)}
                for f in store_all[i]
            ],
            "has_prior_thread": bool(ctx.get("prior")),
            "draft": (d or {}).get("body"),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, n)) as ex:
        rows = list(ex.map(_row, range(len(ctxs))))
    return {
        "user_id": user_id,
        "voice_examples_loaded": bool((voice_block or "").strip()),
        "count": len(rows),
        "drafts": rows,
    }


@router.get("/_diagnostics")
def book_diagnostics(_: None = Depends(_require_admin_token)):
    """At-a-glance ops health in ONE call (absorbed the old `_status` and
    `_updates-status` endpoints). Token-gated; in-memory + per-replica, so hit
    it a couple times for a fuller multi-replica picture.

      status  — request counts, recent errors / slow requests, Claude-call
                stats, and live rate-gate state (is the relationship layer
                throttling right now).
      updates — updates-engine cutover diagnostic: is Bright Data configured,
                what did the last sweep do (exa vs brightdata), and what fields
                did the last delivery parse (hit right after a run to validate
                field-mapping).

        curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \\
             https://event.surpluslayer.com/api/book/_diagnostics | jq
    """
    from .. import metrics
    from ..agents.relationship.updates_engine import status
    return {"status": metrics.snapshot(), "updates": status()}


@router.post("/_updates-test")
def updates_test_endpoint(url: str, _: None = Depends(_require_admin_token)):
    """Fire a Bright Data scrape for ONE LinkedIn url (validation). Returns the
    immediate trigger outcome (status/response); the scraped data arrives async at
    /webhooks/brightdata — then GET /_diagnostics to see last_delivery and
    validate the field mapping. Cheap: one record on the free credits."""
    from ..providers import brightdata
    ok = brightdata.trigger_updates([url])
    return {"triggered": ok, "last_trigger": brightdata.last_trigger()}


# ═════════════════════════════════════════════════════════════════════════════
# /api/relationships — the durable contact spine (formerly routes/relationships.py)
#
# Read API for the event-native relationship layer: surfaces the schema-free
# timeline + summary built by agents/relationship/spine/relationships.py. Every
# route is owner-scoped: a prospect is only reachable by the user who owns its
# event (same 404-on-not-owned discipline as get_owned_event), so relationship
# data never leaks across users.
# ═════════════════════════════════════════════════════════════════════════════

relationships_router = APIRouter(prefix="/api/relationships",
                                 tags=["relationships"])

# Sorts never-touched / timeless relationships to the END when sorting newest
# touch first (reverse=True).
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _enforce_relationship_quota(db: Session, user: models.User) -> None:
    """Roll the user into the current billing period, then HARD-BLOCK (402) if
    they've exhausted their drafting or contact-scan budget for the period.

    Demo + allowlisted accounts bypass entirely (bp.is_unlimited), so live
    demos never hit a wall mid-run. The SPA reads detail.error + detail.redirectTo
    to bounce the user to the pricing table. Kept separate from the legacy
    paid_at LinkedIn-send gate."""
    if bp.ensure_current_period(user):
        db.commit()
    if not bp.can_generate_draft(user):
        raise HTTPException(
            status_code=402,
            detail={"error": "LIMIT_REACHED", "redirectTo": "/billing",
                    "message": "You've used all your follow-up drafts for this "
                               "period. Upgrade to keep going.",
                    "billing": bp.usage_snapshot(user)})
    if not bp.can_scan_contacts(user, 1):
        raise HTTPException(
            status_code=402,
            detail={"error": "CONTACT_LIMIT_REACHED", "redirectTo": "/billing",
                    "message": "You've reached your contact-scan limit for this "
                               "period. Upgrade to scan more.",
                    "billing": bp.usage_snapshot(user)})


def _record_relationship_usage(db: Session, user: models.User, res) -> None:
    """Meter one relationship run: +1 per staged DRAFT card, +contacts_seen for
    the triage scan. Best-effort — a metering failure must never break an
    otherwise-successful run, so we swallow + roll back on error."""
    try:
        drafts = sum(1 for p in res.proposals if p.kind == "draft_message")
        contacts = int(getattr(res, "contacts_seen", 0) or 0)
        if drafts or contacts:
            bp.record_usage(user, drafts=drafts, contacts=contacts)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"  [billing] usage record failed: {type(exc).__name__}: {exc}")
        db.rollback()


def _owned_contact(db: Session, contact_id: int, user: models.User) -> models.Contact:
    """Fetch a Contact, requiring `user` to own it. 404 in both the not-found
    and not-owned cases so we never leak another user's relationship graph."""
    c = db.get(models.Contact, contact_id)
    if c is None or getattr(c, "user_id", None) != user.id:
        raise HTTPException(404, "contact not found")
    return c


def _contact_linkedin_url(db, contact) -> Optional[str]:
    """Best LinkedIn PROFILE url for a contact. Prefer the stored linkedin_url;
    else recover it from the newest activity_update, whose meta_json carries the
    url we found the person's post/job-change at.

    Phone/import-first contacts routinely get LinkedIn activity matched to them
    without a linkedin_url ever landing on the record -- so a send has no handle
    to resolve a provider_id from, and the DM fails even for a real 1st-degree
    connection. We prefer a canonical /in/<handle> profile url; a post url still
    yields the handle, so we keep it as a fallback."""
    direct = getattr(contact, "linkedin_url", None)
    if direct:
        return direct
    rows = (db.query(models.RelationshipInteraction)
              .filter(models.RelationshipInteraction.contact_id == contact.id,
                      models.RelationshipInteraction.source_type == "activity_update")
              .order_by(models.RelationshipInteraction.occurred_at.desc())
              .all())
    fallback = None
    for r in rows:
        try:
            url = (json.loads(r.meta_json or "{}") or {}).get("url") or ""
        except Exception:  # noqa: BLE001
            url = ""
        # Host-parse, not substring: meta urls originate from scraped
        # payloads, and "evil.com/linkedin.com/in/x" must not pass a check
        # that feeds provider_id resolution for real sends.
        if not _is_linkedin_url(url):
            continue
        if "/in/" in (urlparse(url).path or ""):
            return url
        if fallback is None:
            fallback = url
    return fallback


def _is_linkedin_url(url: str) -> bool:
    """True only when the url's HOST is linkedin.com (or a subdomain)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _sendable_prospect(db, contact: models.Contact, user) -> models.Prospect:
    """Resolve a Contact to the per-event Prospect a follow-up acts through
    (send_and_log / scheduling both need prospect.event).

    An import-path contact (LinkedIn/email, no event capture) has NO linked
    prospect -- so instead of a dead-end 409, mint one under the user's durable
    'book' Event. This makes a send work regardless of where the contact came
    from (the same prospect-first seam the Book roster had for last_touch).

    Either way we backfill the prospect's (and contact's) linkedin_url from the
    contact's updates when it is missing, so the DM path can resolve a
    provider_id -- a book-prospect minted before the first update arrived would
    otherwise be stuck with a null handle forever."""
    linked = [p for p in (getattr(contact, "prospects", None) or [])
              if getattr(p, "event", None) is not None]
    if linked:
        linked.sort(key=lambda p: getattr(p, "captured_at", None) or _MIN_DT,
                    reverse=True)
        p = linked[0]
    else:
        # Find-or-create a per-user 'book' event to hang the minted prospect on.
        ev = (db.query(models.Event)
                .filter_by(user_id=user.id, kind="book").first())
        if ev is None:
            ev = models.Event(user_id=user.id, kind="book", label="Your book", city="")
            db.add(ev)
            db.flush()
        p = models.Prospect(
            event_id=ev.id, contact_id=contact.id,
            identity=(getattr(contact, "primary_identity_key", None)
                      or _contact_linkedin_url(db, contact)
                      or getattr(contact, "name", None) or "unknown"),
            name=getattr(contact, "name", None) or "Unknown",
            company=getattr(contact, "company", None) or "Unknown",
            linkedin_url=_contact_linkedin_url(db, contact),
            status="contacted", sources="book",
            captured_at=datetime.now(timezone.utc))
        db.add(p)
        db.commit()
        db.refresh(p)

    # Backfill a LinkedIn handle onto a reused (or freshly minted) prospect that
    # still lacks one, and mirror it onto the contact so it is durable.
    if not p.linkedin_url or not getattr(contact, "linkedin_url", None):
        url = _contact_linkedin_url(db, contact)
        if url:
            if not p.linkedin_url:
                p.linkedin_url = url
            if not getattr(contact, "linkedin_url", None):
                contact.linkedin_url = url
            db.commit()
    return p


def _send_fail_hint(reason, name: str, channel: str = "LinkedIn") -> str:
    """A calm, host-facing message for a failed send. A real send failure (the
    recipient is not a 1st-degree connection yet, the provider declined, a
    transient upstream hiccup) is a business outcome, NOT a server error -- so
    callers raise it as a 409 with this string instead of a 502 that the client
    would mislabel 'the server took too long'. The raw provider reason is kept
    short and appended for debuggability, but the lead is human and actionable."""
    who = (name or "this contact").strip()
    raw = str(reason or "").strip().lower()
    if any(k in raw for k in ("not a relation", "not connected", "not in your network",
                              "invitation", "cannot_resend", "not_connected", "relation")):
        return (f"You're not connected to {who} on LinkedIn yet, so a message can't be "
                f"delivered. Send an invite first, or use Send via Email.")
    return (f"{channel} couldn't deliver this to {who} right now. Try again in a moment, "
            f"or use Send via Email.")


def _fire_booking_after_send(db, user, contact, booking_payload, text: str):
    """Fire the calendar booking a meeting-proposal draft carries, AFTER its
    message sent. Booking is a side effect of SENDING the draft (manual host send
    here; the cron does the auto-send equivalent). Never raises and never affects
    the send's success: a booking miss (no contact email, no open slot) just means
    the message went out without the auto-created invite. Returns the booking
    result dict to surface on the response, or None when there's no payload."""
    if not booking_payload:
        return None
    from ..agents.relationship.pipeline.send.sender import fire_booking_on_send
    try:
        topic = (text or "Quick chat").strip().split("\n", 1)[0][:80] or "Quick chat"
        return fire_booking_on_send(db, user, contact, booking_payload, topic=topic)
    except Exception:  # noqa: BLE001 : a booking miss never fails a sent message
        return None


def _owned_prospect(db: Session, prospect_id: int, user: models.User) -> models.Prospect:
    """Fetch a Prospect, requiring `user` to own its event. 404 in both the
    not-found and not-owned cases so we never leak another user's prospects."""
    p = db.get(models.Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "prospect not found")
    ev = p.event
    if ev is None or getattr(ev, "user_id", None) != user.id:
        raise HTTPException(404, "prospect not found")
    return p


def _prospect_brief(p: models.Prospect) -> dict:
    """Small, safe identity subset : enough for the timeline header, nothing
    sensitive beyond what the CRM already exposes to the host."""
    return {
        "prospect_id": p.id,
        "name": p.name,
        "role": p.role,
        "company": p.company,
        "headline": p.headline,
        "linkedin_url": p.linkedin_url,
        "status": p.status,
        "connection_status": p.connection_status,
        "contact_type": p.contact_type,
        "source": p.source,
        "captured_at": p.captured_at,
    }


class NoteIn(BaseModel):
    summary: str
    title: str = "Note"
    visibility: str = "private"      # "private" | "team"



@relationships_router.post("/email/sync")
def sync_email(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Pull who the user actually corresponds with from their connected
    mailbox into the Contact spine (see agents/email_sync.py). Synchronous —
    a few Unipile pages — so the Integrations tile can await real counts.
    409 until a mailbox is connected. Also auto-kicked once by the email
    connect webhook, so most users never need to call this by hand."""
    if not getattr(user, "unipile_email_account_id", None):
        raise HTTPException(409, "no email account connected")
    from ..agents.relationship.email_sync import sync_email_contacts
    dsn, api_key = _unipile_cfg()
    stats = sync_email_contacts(db, user, dsn=dsn, api_key=api_key)
    return {"ok": stats.get("error") is None, **stats}


class EmailSendIn(BaseModel):
    """One outbound email to a contact, from the host's connected mailbox."""
    message: str
    subject: Optional[str] = None  # default derived from the shared event


@relationships_router.post("/contacts/{contact_id}/send-email")
def send_contact_email(
    contact_id: int,
    body: EmailSendIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send `message` to one owned contact AS AN EMAIL from the host's own
    connected mailbox (their Unipile GOOGLE/OUTLOOK seat — never a shared
    account). The email twin of /contacts/{id}/followup.

    Gates: the contact must have a known email (prospect.email or the
    Contact spine's), and the host must have a connected, active mailbox —
    except in dry-run, where the payload is built but nothing leaves the
    box (demos exercise the full path). The per-channel double-send guard
    applies: an unconfirmed email send blocks a blind retry."""
    require_paid(user)  # email send is a paid action (no LinkedIn needed)
    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")
    prospect = _sendable_prospect(db, contact, user)

    to_address = ((getattr(prospect, "email", None) or "").strip().lower()
                  or (contact.email or "").strip().lower())
    if not to_address:
        raise HTTPException(409, "no email address on file for this contact")

    from ..providers import get_provider_for_user
    provider = get_provider_for_user(user)
    email_account_id = getattr(user, "unipile_email_account_id", None) or ""
    if not provider.dry_run:
        if not email_account_id or \
                getattr(user, "email_status", "") != "active":
            raise HTTPException(
                409, "connect your email in Integrations before sending")

    from ..agents.relationship.pipeline.send.flow import _assert_no_recent_send
    if not provider.dry_run:
        _assert_no_recent_send(db, prospect, channel="email")

    ev = getattr(prospect, "event", None)
    label = (getattr(ev, "label", "") or "").strip() if ev else ""
    subject = (body.subject or "").strip() or (
        f"Great meeting you at {label}" if label else "Great meeting you")

    # PUSH-in-thread: when the host confirmed a thread for this contact,
    # reply to its LATEST message (reply_to + matching 'Re:' subject is
    # Unipile's threading contract) so Gmail/Outlook keep one conversation.
    reply_to = None
    if contact.email_thread_id and not provider.dry_run:
        try:
            from ..agents.relationship.email_sync import thread_messages
            dsn, api_key = _unipile_cfg()
            msgs = thread_messages(
                dsn=dsn, api_key=api_key, account_id=email_account_id,
                thread_id=contact.email_thread_id,
                own_address=getattr(user, "email_account_address", "") or "")
            if msgs:
                last = msgs[-1]
                reply_to = last.get("provider_id")
                orig = (last.get("subject") or "").strip()
                if orig:
                    subject = orig if orig.lower().startswith("re:") \
                        else f"Re: {orig}"
        except Exception as exc:  # noqa: BLE001 : fall back to a fresh email
            print(f"  [email.send] thread lookup failed, sending fresh: "
                  f"{type(exc).__name__}: {exc}")

    from ..agents.relationship.email_sync import format_email_html
    to_first = ((contact.name or prospect.name or "").split() or [""])[0]
    host_first = ((user.name or "").split() or [""])[0]
    res = provider.send_email(
        email_account_id=email_account_id,
        to_address=to_address,
        to_name=(contact.name or prospect.name or ""),
        subject=subject,
        body=format_email_html(text, to_first, host_first),
        prospect_id=prospect.id,
        reply_to=reply_to,
    )

    # Truthful log on the email channel : message_sent / unconfirmed / failed
    # (dry runs log dry_run_queued). Same discipline as sender.send_and_log.
    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="email",
        state=res.state,
        body=f"[{subject}] {text}"[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    db.commit()

    if res.error and res.state == "failed":
        raise HTTPException(409, _send_fail_hint(res.error, contact.name, "Email"))
    return {"status": "unconfirmed" if res.state == "unconfirmed" else "sent",
            "dry_run": res.dry_run, "contact_id": contact_id,
            "prospect_id": prospect.id, "to": to_address, "subject": subject}


def _unipile_cfg() -> tuple[str, str]:
    creds = unipile_creds()
    if not creds:
        raise HTTPException(503, "Unipile not configured")
    return creds


def _email_channel_ready(user) -> str:
    """The user's email account id, 409ing when no mailbox is connected."""
    acct = getattr(user, "unipile_email_account_id", None)
    if not acct or getattr(user, "email_status", "") != "active":
        raise HTTPException(409, "connect your email in Integrations first")
    return acct


@relationships_router.get("/contacts/{contact_id}/email-threads")
def list_contact_email_threads(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Candidate mailbox threads with this contact's address — what the host
    picks from to CONFIRM 'this is my thread with them'. Manual by design:
    we never guess the thread, the host links it."""
    contact = _owned_contact(db, contact_id, user)
    if not (contact.email or "").strip():
        raise HTTPException(409, "no email address on file for this contact")
    acct = _email_channel_ready(user)
    dsn, api_key = _unipile_cfg()
    from ..agents.relationship.email_sync import list_threads_for_address
    threads = list_threads_for_address(
        dsn=dsn, api_key=api_key, account_id=acct,
        address=contact.email.strip().lower(),
        own_address=getattr(user, "email_account_address", "") or "")
    return {"contact_id": contact_id, "address": contact.email,
            "linked_thread_id": contact.email_thread_id, "threads": threads}


class ContactEmailIn(BaseModel):
    email: Optional[str] = None  # null clears


@relationships_router.post("/contacts/{contact_id}/email")
def set_contact_email(
    contact_id: int,
    body: ContactEmailIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Manually attach (or clear) this contact's email address — the host
    types it in on the contact view. This is THE entry point of the email
    channel for a contact: thread listing, pull, and push all key off it.
    Changing the address clears any linked thread (it belonged to the old
    address). Also backfills the linked prospects so capture-side surfaces
    (and link_contact identity) see it."""
    contact = _owned_contact(db, contact_id, user)
    addr = (body.email or "").strip().lower() or None
    if addr is not None and "@" not in addr:
        raise HTTPException(422, "that doesn't look like an email address")
    if addr != (contact.email or None):
        contact.email_thread_id = None  # old thread belonged to the old address
    contact.email = addr
    for p in (getattr(contact, "prospects", None) or []):
        if addr and not getattr(p, "email", None):
            p.email = addr
    db.commit()
    return {"contact_id": contact_id, "email": contact.email,
            "linked_thread_id": contact.email_thread_id}


class StarIn(BaseModel):
    vip: Optional[bool] = None  # null = toggle; true/false = set explicitly


@relationships_router.post("/import-conversations", status_code=200)
def import_conversations(
    want: int = 15,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Seed the Book from the user's genuine LinkedIn DM conversations (people
    they actually replied to and had an active back-and-forth with). Idempotent
    -- re-runs only add new people. Uses the user's OWN connected account.

    Used to run the whole import INLINE : up to 80 chats x 3 sequential
    Unipile GETs each (12s timeout apiece) before this returned, so the button
    could spin for minutes. Now it queues a Job and returns the id
    immediately; the work runs detached (jobs.execute_import_conversations, on
    its own DB session) and the frontend polls
    GET /import-conversations/{job_id} for progress + the final stats."""
    from .. import jobs as jobs_mod
    job = jobs_mod.new_job(db, event_id=None, user_id=user.id,
                           kind="import_conversations")
    # prefer_modal : the walk can take minutes, so let it survive a web-worker
    # recycle when USE_MODAL is on (local daemon thread otherwise).
    runner = jobs_mod.run_detached(
        jobs_mod.execute_import_conversations, job.id,
        prefer_modal=True,
        want=max(1, min(want, 30)))
    return {"job_id": job.id, "status": "queued", "runner": runner}


@relationships_router.get("/import-conversations/{job_id}")
def import_conversations_status(
    job_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Poll one import job. Owner-scoped : 404 unless the job belongs to the
    requesting user and is a conversation import. While running, `progress`
    carries {scanned, found} beats from the chat walk; when done, `result`
    carries the import stats ({imported, considered, reason?})."""
    job = db.get(models.Job, job_id)
    if (job is None or job.kind != "import_conversations"
            or job.user_id != user.id):
        raise HTTPException(404, "import job not found")
    out: dict = {"job_id": job.id, "status": job.status}
    if job.status == "done" and job.result_json:
        try:
            out["result"] = json.loads(job.result_json)
        except (ValueError, TypeError):
            # A truncated / corrupt result_json must not 500 the poller ; hand
            # back the raw payload so the import screen can still resolve.
            out["result_raw"] = job.result_json
    elif job.status in ("queued", "running") and job.result_json:
        try:
            out["progress"] = json.loads(job.result_json)
        except ValueError:
            # Mid-write progress JSON can be momentarily truncated; the poller
            # simply shows no progress this tick rather than 500ing.
            pass
    if job.status == "error":
        out["error"] = job.error
    return out


class ChannelIn(BaseModel):
    channel: Optional[str] = None  # "email" | "linkedin" | null (auto)


@relationships_router.post("/contacts/{contact_id}/channel")
def set_contact_channel(
    contact_id: int,
    body: ChannelIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Set which channel to follow up with this contact on. Drafts + sends honor
    it (email pulls the real email thread for context). null = auto-default."""
    contact = _owned_contact(db, contact_id, user)
    ch = (body.channel or "").strip().lower() or None
    if ch not in (None, "email", "linkedin"):
        raise HTTPException(400, "channel must be 'email', 'linkedin', or null")
    contact.preferred_channel = ch
    db.commit()
    return {"contact_id": contact_id, "preferred_channel": ch}


def _kick_vip_scrape(db, contact_id: int) -> None:
    """Detached one-off scrape for a just-starred contact (run via
    jobs.run_detached). Top-level so it stays importable; `db` is the
    run_detached-owned session. Best-effort."""
    from ..agents.relationship.updates_engine import scrape_contact
    c = db.get(models.Contact, contact_id)
    if c is not None:
        print(f"[star] kicked scrape for contact={contact_id}: "
              f"{scrape_contact(db, c)}", flush=True)


@relationships_router.post("/contacts/{contact_id}/star")
def set_contact_star(
    contact_id: int,
    body: StarIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Star / unstar a contact. Starred (⭐ vip) contacts are monitored more
    often by the updates engine. `vip` null → toggle; true/false → set."""
    contact = _owned_contact(db, contact_id, user)
    contact.vip = (not contact.vip) if body.vip is None else bool(body.vip)
    db.commit()
    # On star (not unstar), kick a one-off update check in the background so
    # close-monitoring starts now instead of waiting for the next sweep. Best
    # effort, its own session; never blocks or fails the toggle.
    if contact.vip and (contact.linkedin_url or "").strip():
        from ..jobs import run_detached
        run_detached(_kick_vip_scrape, contact_id)
    return {"contact_id": contact_id, "vip": contact.vip}


class ThreadLinkIn(BaseModel):
    thread_id: Optional[str] = None  # null unlinks


@relationships_router.post("/contacts/{contact_id}/email-thread")
def link_contact_email_thread(
    contact_id: int,
    body: ThreadLinkIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The host's manual confirmation: set (or clear) the ONE mailbox thread
    that belongs to this contact. Pull and push both key off it."""
    contact = _owned_contact(db, contact_id, user)
    contact.email_thread_id = (body.thread_id or "").strip() or None
    db.commit()
    return {"contact_id": contact_id,
            "linked_thread_id": contact.email_thread_id}


@relationships_router.get("/contacts/{contact_id}/email-thread")
def read_contact_email_thread(
    contact_id: int,
    with_bodies: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """PULL: the linked thread's messages, oldest first (live read from the
    mailbox — nothing stored). 409 until the host has linked a thread."""
    contact = _owned_contact(db, contact_id, user)
    if not contact.email_thread_id:
        raise HTTPException(409, "no email thread linked for this contact")
    acct = _email_channel_ready(user)
    dsn, api_key = _unipile_cfg()
    from ..agents.relationship.email_sync import thread_messages
    msgs = thread_messages(
        dsn=dsn, api_key=api_key, account_id=acct,
        thread_id=contact.email_thread_id,
        own_address=getattr(user, "email_account_address", "") or "",
        with_bodies=with_bodies)
    return {"contact_id": contact_id,
            "thread_id": contact.email_thread_id, "messages": msgs}


@relationships_router.get("/contacts")
def list_contacts(
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """The durable 'who I've met' inventory : one row per Contact (the cross-event
    person), rolled up over every event we've shared with them. This is the
    contact-centric counterpart to /prospects (which is per-event-record).

    Owner-scoped : only the caller's own Contacts are reachable. Newest touch
    first, so the people you've engaged most recently surface at the top.
    """
    # Eager-loaded contacts (prospects/event/outreach/conversion in ~5 queries)
    # + a single batched interaction prefetch, so the rollup below is pure
    # in-memory work instead of ~5 queries per prospect (the N+1 that made this
    # page take tens of seconds for a contact-rich user).
    contacts = rel_agent.list_contacts(db, user.id)
    inter_index = rel_agent.prefetch_interactions_by_prospect(db, contacts)
    update_index = rel_agent.prefetch_activity_updates_by_contact(db, contacts)
    rows = [rel_agent.contact_summary(db, c, inter_index,
                                          update_index.get(c.id))
            for c in contacts]
    # "What's new on top" : order by the freshest signal — the most recent
    # external update if there is one, else the last touch — so contacts the
    # poller just found news about surface first.
    def _freshness(r):
        upd = (r.get("latest_update") or {}).get("occurred_at")
        return max(d for d in (upd, r["last_touch_at"], _MIN_DT) if d is not None)
    rows.sort(key=_freshness, reverse=True)
    return {"count": len(rows), "contacts": rows}


@relationships_router.delete("/contacts/{contact_id}")
def delete_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Permanently remove a person from the book. Owner-scoped (404 on someone
    else's or a missing contact). FK cascade drops the person's children
    (identities, facts, interactions, outgoing messages). Any Prospect rows that
    pointed at this contact are UNLINKED (contact_id -> NULL) rather than deleted,
    so per-event history the person appeared in is preserved."""
    contact = _owned_contact(db, contact_id, user)
    for p in (db.query(models.Prospect)
              .filter(models.Prospect.contact_id == contact.id).all()):
        p.contact_id = None
    db.flush()
    db.delete(contact)
    db.commit()
    print(f"  [relationships.delete_contact] user={user.id} removed contact={contact_id}",
          flush=True)
    return {"ok": True, "deleted_contact_id": contact_id}






@relationships_router.post("/contacts/{contact_id}/snooze")
def snooze_contact_endpoint(
    contact_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Dismiss a contact from the cadence due-feed for `days` ('not now', no send).
    Their dated triggers (birthday) still surface. Owner-scoped (404 if not owned)."""
    _owned_contact(db, contact_id, user)
    from ..agents.relationship.pipeline.proactive import cadence
    return cadence.snooze_contact(db, user.id, contact_id, days=days)


@relationships_router.delete("/contacts/{contact_id}/snooze")
def unsnooze_contact_endpoint(
    contact_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Clear a contact's cadence snooze so they can surface again. Owner-scoped."""
    _owned_contact(db, contact_id, user)
    from ..agents.relationship.pipeline.proactive import cadence
    return {"cleared": cadence.unsnooze_contact(db, user.id, contact_id)}







class ChatIn(BaseModel):
    """One turn from the host's follow-up chat. `message` is the host's ask
    ('who should I follow up with?', 'draft a ping to anyone at Stripe')."""
    message: str = ""


@relationships_router.post("/chat")
def relationship_chat(
    body: ChatIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Conversational front door to the propose-only relationship agent.
    (Non-stream variant: superseded in the UI by /chat/stream, KEPT as the
    synchronous seam the relationship-billing tests drive directly — quota
    enforcement + usage recording assert against its return value.)

    The host types an ask; we steer the same auditable survey-and-propose loop
    with it and hand back (a) a one-paragraph natural-language reply and (b) the
    staged proposals (each a contact + drafted follow-up + rationale). NOTHING
    is sent here: the host approves a draft separately via the followup route,
    which is where the send-vs-draft decision is made. Owner-scoped."""
    _enforce_relationship_quota(db, user)
    from ..agents.relationship.pipeline.agent.run import (
        run_relationship_agent_concurrent as _run)
    res = _run(db, user.id, instruction=(body.message or "").strip())
    _record_relationship_usage(db, user, res)
    out = res.as_dict()
    # Surface the send-on-approve preference so the chat can label the approve
    # button correctly ("Send now" when on, "Save draft" when off) without a
    # second round-trip. Reads the LEGACY per-user column (nothing writes it
    # anymore, so this is False for new users -> approve stages a draft).
    out["auto_send_enabled"] = bool(getattr(user, "auto_followups_enabled", False))
    return out


# How often to trickle a keepalive comment while the agent is mid-think and has
# no frame to send. Must stay well under the edge proxy's idle timeout (~30s+ on
# Railway/Cloudflare) so a silent stream never gets cut with a 502.
_HEARTBEAT_SECS = 10


def _sse(event: str, data: dict) -> str:
    """One Server-Sent-Events frame: an event name + a JSON data line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _drain_stream(q: "queue.Queue", *, heartbeat_secs: float = _HEARTBEAT_SECS):
    """Yield SSE bytes off the worker queue until the sentinel (None, None).

    During a silence (the agent mid-think, nothing staged yet) trickle a
    keepalive comment every `heartbeat_secs` so the connection never goes quiet
    long enough for an edge proxy to idle-time-out and 502 the browser. Comment
    frames (": ...") carry no event:/data: line, so the client parser drops them.
    """
    while True:
        try:
            event, data = q.get(timeout=heartbeat_secs)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if event is None:
            return
        yield _sse(event, data)


@relationships_router.post("/chat/stream")
def relationship_chat_stream(
    body: ChatIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Streaming twin of /chat: same propose-only loop, but each drafted
    follow-up is pushed to the client the instant the agent stages it (SSE),
    so the chat reveals people one-by-one as the survey runs instead of
    freezing on a spinner until the whole loop finishes.

    Frames: `meta` (auto-send pref, sent first) -> `proposal` (one per staged
    draft) -> `done` (closing summary) -> `error` (if the run blew up). Still
    NOTHING is sent here; proposals are staged suggestions only. Owner-scoped.

    The agent runs in a worker thread with its OWN DB session (the request's
    session can't cross threads), pushing onto a queue the SSE generator drains.
    user.id is captured up front so the thread never touches the request user.

    The agent runs the two-phase concurrent variant: one triage call, then a
    bounded parallel fan-out of per-person drafts, each card streaming the moment
    its draft resolves. Cards arrive in completion order (not strict priority),
    which is the trade for collapsing time-to-all-cards from Σ to ~max."""
    # Pre-flight the paywall BEFORE we open the stream: a 402 here is a clean
    # JSON error the SPA can redirect on, whereas raising mid-stream would only
    # surface as an SSE `error` frame after the connection is already open.
    _enforce_relationship_quota(db, user)

    from ..agents.relationship.pipeline.agent.run import (
        run_relationship_agent_concurrent as _run)

    user_id = user.id
    auto = bool(getattr(user, "auto_followups_enabled", False))
    instruction = (body.message or "").strip()
    q: "queue.Queue" = queue.Queue()
    # Set when the client goes away (or the stream completes). The agent
    # checks it before every Claude call, so a closed tab stops the run at
    # the next call boundary instead of silently burning tokens + a DB
    # session to the end of the fan-out.
    stop = threading.Event()

    def _worker():
        from ..agents.relationship.followup_scheduler import suggest_send_time
        db = SessionLocal()
        # One sensible default fire time for this batch; the card prefills its
        # picker with it and the host overrides freely.
        suggested = suggest_send_time().isoformat()
        try:
            def _emit(p):
                if stop.is_set():
                    return  # nobody is reading; don't grow the queue
                q.put(("proposal", {
                    "kind": p.kind, "contact_id": p.contact_id,
                    "contact_name": p.contact_name, "text": p.text,
                    "rationale": p.rationale,
                    "suggested_send_at": suggested,
                }))
            res = _run(db, user_id, instruction=instruction,
                       on_proposal=_emit, stop_event=stop)
            # Meter on the worker's own session/row (the request user can't
            # cross threads). Best-effort; never fails the completed run.
            worker_user = db.get(models.User, user_id)
            if worker_user is not None:
                _record_relationship_usage(db, worker_user, res)
            q.put(("done", {"summary": res.summary or "Done.",
                            "auto_send_enabled": auto,
                            "network_hits": list(res.network_hits)}))
        except Exception as exc:  # noqa: BLE001 : surface to the client, don't 500 mid-stream
            q.put(("error", {"message": str(exc)}))
        finally:
            db.close()
            q.put((None, None))

    def _stream():
        # The finally runs on normal completion AND on GeneratorExit — which
        # is what Starlette throws into the generator when the client
        # disconnects mid-stream. Either way, tell the worker to wind down.
        try:
            yield _sse("meta", {"auto_send_enabled": auto})
            threading.Thread(target=_worker, daemon=True).start()
            yield from _drain_stream(q)
        finally:
            stop.set()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        # Defeat proxy buffering so frames arrive as they're produced.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FollowupSendIn(BaseModel):
    """Approve one drafted follow-up for a contact. `message` is the (possibly
    host-edited) body to act on. `channel` picks the transport: "linkedin"
    (default, the historical behavior) or "email" — which routes through the
    contact's stored address + linked thread, no manual typing."""
    message: str
    channel: str = "linkedin"
    subject: Optional[str] = None  # email-only; default derived/Re: threaded


@relationships_router.post("/contacts/{contact_id}/followup")
def send_contact_followup(
    contact_id: int,
    body: FollowupSendIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Send an approved follow-up draft for one owned contact, immediately.

    Approve = send: this is a manual, user-initiated action, so it bypasses the
    autonomy gates (which only govern unattended sends) and goes out through the
    shared send path (DRY_RUN / paywall enforced inside the provider, exactly
    like the dispatcher). Returns status='sent'.

    Owner-scoped (404 on not-owned contact). The contact is resolved to a
    sendable Prospect by picking its most-recently captured linked prospect."""
    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")

    # Email transport : same approve flow, different wire. Routes through
    # the contact's STORED address (+ linked thread when confirmed), so the
    # agent's drafts can go out as email without the host typing anything.
    if (body.channel or "linkedin").lower() == "email":
        return send_contact_email(
            contact_id, EmailSendIn(message=text, subject=body.subject),
            db, user)

    require_can_send_linkedin(user)  # LinkedIn send paywall (bypasses unlimited)
    prospect = _sendable_prospect(db, contact, user)

    # Approving a specific message IS the user deciding: a manual send, so it
    # always sends (the autonomy gates only govern UNATTENDED sends). The old
    # legacy-column branch quietly staged a private note instead -- an approve
    # button that does not send. Flipped 2026-07-01 per Daniel.
    from ..agents.relationship.pipeline.send.sender import send_followup
    try:
        res = send_followup(db, prospect, text, channel="linkedin")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, _send_fail_hint(exc, contact.name)) from exc
    if getattr(res, "error", None):
        raise HTTPException(409, _send_fail_hint(res.error, contact.name))
    return {"status": "sent", "contact_id": contact_id,
            "prospect_id": prospect.id, "message": text}


class FollowupScheduleIn(BaseModel):
    """Schedule (or immediately send) a chat-drafted follow-up for a contact.

    `message` is the (possibly host-edited) body. `send_at` is the host-chosen
    fire time; null/absent or a past time means 'send now'. This is the
    Gmail-style 'Schedule send' the chat cards drive."""
    message: str
    send_at: Optional[datetime] = None
    channel: str = "linkedin"  # "linkedin" | "email"
    # Structured booking intent for a MEETING-PROPOSAL draft (see
    # integrations.booking.propose_meeting_slot). When present, SENDING this draft
    # also fires the calendar booking: a "propose_time" payload creates the event +
    # invites the contact; a "calendly" payload is self-serve (the link is in the
    # body) so it fires nothing. None for an ordinary follow-up.
    booking_payload: Optional[dict] = None


@relationships_router.post("/contacts/{contact_id}/schedule")
def schedule_contact_followup(
    contact_id: int,
    body: FollowupScheduleIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Approve a chat-drafted follow-up by SCHEDULING it (or sending now).

    Bridges the propose-only relationship chat into the existing ScheduledFollowup
    queue, so a drafted message becomes a real timed send instead of a dead-end
    private note:

      send_at now/past/absent -> send immediately (send_and_log), status='sent'.
      send_at in the future    -> upsert the prospect's pending ScheduledFollowup
                                   to body + send_at, status='scheduled'.

    A SCHEDULED row is auto-fired by the dispatcher only when the general-send
    master (SURPLUS_AUTOMATED_SENDS + channel allowlist) is on; off leaves it
    queued for a manual send-now. We surface `auto_send_enabled` (the legacy
    per-user column) so the card can say 'will send automatically' vs 'queued
    for your confirmation'. An immediate
    'send now' is an explicit host action and always sends. Owner-scoped."""
    from ..agents.relationship.followup_scheduler import pending_followup

    contact = _owned_contact(db, contact_id, user)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(422, "message is required")
    prospect = _sendable_prospect(db, contact, user)

    now = datetime.now(timezone.utc)
    send_at = body.send_at
    if send_at is not None and send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=timezone.utc)

    # Send now: no future time chosen. Explicit host action, sends regardless of
    # the auto toggle (same as the followups send-now route).
    want_email = (getattr(body, "channel", "") or "linkedin") == "email"
    # Send paywall, per channel -- gates both an immediate send and a scheduled
    # one (queuing a paid send still needs a paid account). Bypasses unlimited.
    if want_email:
        require_paid(user)
    else:
        require_can_send_linkedin(user)
    booking_payload = getattr(body, "booking_payload", None)
    if send_at is None or send_at <= now:
        from ..agents.relationship.pipeline.send.sender import send_followup
        if want_email:
            try:
                res = send_followup(db, prospect, text, channel="email")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            db.commit()
            if res.error and res.state == "failed":
                raise HTTPException(409, _send_fail_hint(res.error, contact.name, "Email"))
            booked = _fire_booking_after_send(db, user, contact, booking_payload, text)
            return {"status": "sent", "contact_id": contact_id,
                    "prospect_id": prospect.id, "channel": "email",
                    "dry_run": res.dry_run, **({"booking": booked} if booked else {})}
        try:
            res = send_followup(db, prospect, text, channel="linkedin")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, _send_fail_hint(exc, contact.name)) from exc
        if getattr(res, "error", None):
            raise HTTPException(409, _send_fail_hint(res.error, contact.name))
        booked = _fire_booking_after_send(db, user, contact, booking_payload, text)
        return {"status": "sent", "contact_id": contact_id,
                "prospect_id": prospect.id, "message": text,
                **({"booking": booked} if booked else {})}

    # Schedule: upsert the prospect's one pending row (idempotent per prospect,
    # mirroring stage_followup) so re-approving just reschedules instead of
    # stacking duplicates.
    import json as _json
    payload_str = _json.dumps(booking_payload) if booking_payload else None
    row = pending_followup(db, prospect.id)
    if row is None:
        row = models.ScheduledFollowup(
            prospect_id=prospect.id, body=text, send_at=send_at,
            suggested_send_at=send_at, status="scheduled",
            booking_payload=payload_str)
        db.add(row)
    else:
        row.body = text
        row.send_at = send_at
        row.updated_at = now
        row.booking_payload = payload_str
    row.channel = "email" if want_email else "linkedin"
    db.commit()
    db.refresh(row)
    return {"status": "scheduled", "contact_id": contact_id,
            "prospect_id": prospect.id, "followup_id": row.id,
            "send_at": row.send_at.isoformat(),
            "auto_send_enabled": bool(getattr(user, "auto_followups_enabled", False)),
            "message": text}
