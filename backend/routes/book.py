"""
routes/book.py : the advisor "Your book today" surface (BookApp).

Serves the Today feed (Updates + Needs-outreach), drafts the note behind a
"Draft" tap, and answers the agent ask bar — all backed by agents/book.py.

The feed is built from a DEMO BOOK (the advisor's roster) so the surface renders
end-to-end without a populated relationship spine; agents/book.py runs the real
LLM prompts over it when ANTHROPIC_API_KEY is set, and a deterministic heuristic
otherwise. Wiring the demo book to live Contacts is the next slice.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents import book as book_agent
from ..agents import relationships as rel_agent
from ..auth import current_user
from ..db import get_db

# Reuse the agent's stdout tracer so route- and agent-level [book] lines
# interleave in one Railway stream (grep `[book]`).
_trace = book_agent._btrace

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
         "interaction_history": "Just met — exchanged badges at the afterparty."},
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
    t_upd = time.monotonic() - t
    rel_agent._spine_prof_reset()
    t = time.monotonic()
    out = _book_from_spine_contacts(db, user, contacts, inter_index, update_index)
    t_loop = time.monotonic() - t
    prof = rel_agent.spine_prof()
    _trace(f"_book_from_spine {len(contacts)} contacts: list={t_list:.2f}s "
           f"prefetch_inter={t_inter:.2f}s prefetch_upd={t_upd:.2f}s "
           f"summary_loop={t_loop:.2f}s "
           f"(events={prof['events']:.2f}s timeline={prof['timeline']:.2f}s)")
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
    book = _book_from_spine_contacts(db, user, [match], inter_index, update_index)
    return book[0] if book else None


def _book_from_spine_contacts(db, user, contacts, inter_index, update_index):
    """Inner loop of _book_from_spine, reusable for single-contact fast path."""
    now = datetime.now(timezone.utc)
    book = []
    for c in contacts:
        row = rel_agent.contact_summary(db, c, inter_index, update_index.get(c.id))
        days = 0
        last = row.get("last_touch_at")
        if last is not None:
            try:
                dt = last if isinstance(last, datetime) else datetime.fromisoformat(str(last))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = max(0, (now - dt).days)
            except Exception:
                days = 0
        upd = row.get("latest_update") or {}
        headline = upd.get("title") or upd.get("summary")
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
            }
        identity = row.get("identity") or {}
        book.append({
            "id": str(row.get("contact_id")),
            "name": _real(row.get("name")) or "Unknown",
            "vip": False,
            "title": _real(identity.get("headline")) or _real(identity.get("role")),
            "firm": _real(row.get("company")) or _real(identity.get("company")),
            "tier": "core",
            "days_since": days,
            "cadence_days": 30,
            "review_due": False,
            "met_at": row.get("met_at") or "",
            "value": "",
            "is_prospect": not row.get("is_connection"),
            "stage": row.get("relationship_stage"),
            "interaction_history": row.get("next_step") or "",
            "raw_signals": signals,
        })
    return book


def _load_book(db: Session, user: models.User) -> list[dict]:
    """Real book from the spine; only DEMO users fall back to the demo roster
    (a real account with an empty spine gets an empty book, not fake clients)."""
    t0 = time.monotonic()
    book = _book_from_spine(db, user)
    if book:
        src = "spine"
    else:
        from ..auth import is_demo_user
        book = _demo_book() if is_demo_user(user) else []
        src = "demo" if book else "empty"
    _trace(f"load_book user={user.id} -> {len(book)} contacts ({src}) "
           f"in {time.monotonic()-t0:.2f}s")
    return book


def _advisor_identity(user: models.User) -> tuple[str, str]:
    name = (getattr(user, "name", None) or "").strip() or "your advisor"
    return name, "wealth advisor"


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


# ─── routes ──────────────────────────────────────────────────────────────────

@router.get("/today")
def today(db: Session = Depends(get_db),
          user: models.User = Depends(current_user)):
    """The cached-shape Today feed : time-ordered Updates + priority-ranked
    Needs-outreach. Built by running detection + scoring across the book."""
    t0 = time.monotonic()
    book = _load_book(db, user)
    feed = book_agent.build_today(book)
    name, role = _advisor_identity(user)
    feed["advisor_name"] = name
    # Warm drafts in the background for the people this feed is about to tell
    # the user to contact, so the draft panel is usually instant on tap.
    by_id = {c.get("id"): c for c in book}
    pairs = [(by_id[r["contact_id"]], r.get("trigger") or "catching up")
             for r in feed["needs_outreach"] + feed["updates"]
             if r.get("contact_id") in by_id and (r.get("can_draft") is not False)]
    book_agent.predraft(pairs, user_name=name, user_role=role)
    _trace(f"GET /today user={user.id}: {len(feed['updates'])} updates, "
           f"{len(feed['needs_outreach'])} needs-outreach, predraft {len(pairs)} "
           f"in {time.monotonic()-t0:.2f}s")
    return feed


@router.post("/refresh")
def refresh(db: Session = Depends(get_db),
            user: models.User = Depends(current_user)):
    """Re-run the batch over the book. Same shape as /today; busts the
    assessment cache so the next loads pick up fresh LLM verdicts."""
    _trace(f"POST /refresh user={user.id}: busting assessment+draft caches")
    book_agent.invalidate_assessments()
    return today(db, user)


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
    name, role = _advisor_identity(user)
    t0 = time.monotonic()
    # Consolidated path: when this maps to a real Contact, draft through the ONE
    # shared composer (voice + real prior-message thread + no em dashes). Falls
    # back to the book heuristic drafter for demo-book slugs or on any miss.
    msg = None
    engine = "shared"
    contact_orm = _find_contact_orm(db, user, body.contact_id)
    if contact_orm is not None:
        from ..agents import drafting
        msg = drafting.compose_followup(
            db, user.id, contact_orm, reason=body.trigger, channel=body.channel)
    if msg is None:
        engine = "heuristic"
        msg = book_agent.draft_message_cached(
            contact, body.trigger, channel=body.channel,
            user_name=name, user_role=role)
    _trace(f"POST /draft user={user.id} to={contact.get('name')!r} "
           f"channel={body.channel} trigger={body.trigger!r} engine={engine} "
           f"in {time.monotonic()-t0:.2f}s")
    return {"channel": body.channel, **msg}


@router.post("/ask")
def ask(body: AskIn, db: Session = Depends(get_db),
        user: models.User = Depends(current_user)):
    """The 'Ask your agent anything' bar + chip queries."""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(422, "query is required")
    t0 = time.monotonic()
    book = _load_book(db, user)
    res = book_agent.ask_agent(book, q)        # selection only: draft=null
    people = res.get("people") or []
    # Backfill each selected person with a real voice + thread draft via the ONE
    # shared composer. Resolve Contact ORMs ONCE (list_contacts is expensive),
    # then fan the drafts out concurrently. Cards still show drafts inline.
    orm_by_id = {str(c.id): c for c in rel_agent.list_contacts(db, user.id)}
    by_name = {(c.get("name") or "").strip().lower(): c for c in book}
    jobs, idxs = [], []
    for i, p in enumerate(people):
        bd = by_name.get((p.get("name") or "").strip().lower())
        # Carry the real contact_id onto the card so its Draft sheet can Send /
        # Schedule (those endpoints are contact-id keyed).
        if bd and bd.get("id"):
            p["contact_id"] = bd["id"]
        orm = orm_by_id.get(str(bd.get("id"))) if bd else None
        if orm is not None:
            jobs.append({"contact": orm,
                         "reason": p.get("reason") or "following up",
                         "channel": "email"})
            idxs.append(i)
    if jobs:
        from ..agents import drafting
        drafts = drafting.compose_batch(db, user.id, jobs)
        for j, i in enumerate(idxs):
            d = drafts[j]
            if d and (d.get("body") or "").strip():
                people[i]["draft"] = d["body"]
    res["people"] = people
    drafted = sum(1 for p in people if (p.get("draft") or "").strip())
    _trace(f"POST /ask user={user.id} q={q!r} -> {len(people)} people "
           f"({drafted} drafted via shared composer) in {time.monotonic()-t0:.2f}s")
    return res


@router.get("/relationship/{contact_id}")
def relationship(contact_id: str, db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)):
    """The relationship detail screen : health, the plain-language 'why', the
    relationship value, and a synthesized timeline. The drafted message is
    fetched separately via /draft so it can be refined independently."""
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
    _trace(f"GET /relationship/{contact_id} user={user.id} "
           f"({'fast' if fast else 'full-book'}) in {time.monotonic()-t0:.2f}s")
    return detail
