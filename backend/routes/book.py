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


def _book_from_spine(db: Session, user: models.User) -> list[dict]:
    """Map the real Contact spine (the cross-event 'who I've met' rollup) into
    the book shape the agent prompts expect. Empty when the user has no
    contacts yet — the caller falls back to the demo book so the surface still
    renders end-to-end."""
    contacts = rel_agent.list_contacts(db, user.id)
    if not contacts:
        return []
    inter_index = rel_agent.prefetch_interactions_by_prospect(db, contacts)
    update_index = rel_agent.prefetch_activity_updates_by_contact(db, contacts)
    now = datetime.now(timezone.utc)
    book: list[dict] = []
    for c in contacts:
        row = rel_agent.contact_summary(db, c, inter_index,
                                        update_index.get(c.id))
        # Days since the freshest touch; a never-touched capture counts as new
        # (0 days) rather than dormant.
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
        # The freshest watch-poller change becomes the raw signal feeding the
        # Updates feed (prompt 2 / its heuristic).
        upd = row.get("latest_update") or {}
        headline = upd.get("title") or upd.get("summary")
        signals = None
        if headline:
            occurred = upd.get("occurred_at")
            signals = {
                "type": upd.get("type") or "company_news",
                "headline": headline,
                "detected_at": (occurred.isoformat()
                                if isinstance(occurred, datetime)
                                else (occurred or _ago())),
                "significance": "medium",
                "outreach_trigger": True,
            }
        identity = row.get("identity") or {}
        book.append({
            "id": str(row.get("contact_id")),
            "name": row.get("name") or "Unknown",
            "vip": False,
            "title": identity.get("headline") or "",
            "firm": row.get("company") or identity.get("company") or "",
            "tier": "core",
            "days_since": days,
            "cadence_days": 30,
            "review_due": False,
            "met_at": "",
            "value": "",
            "is_prospect": not row.get("is_connection"),
            # Outreach pipeline stage (captured -> contacted -> replied ->
            # converted / stale) — shown as the chip on the Book rows.
            "stage": row.get("relationship_stage"),
            "interaction_history": row.get("next_step") or "",
            "raw_signals": signals,
        })
    return book


def _load_book(db: Session, user: models.User) -> list[dict]:
    """The caller's real book when the spine has people in it, else the demo
    roster (so a brand-new account still sees a working surface)."""
    book = _book_from_spine(db, user)
    return book if book else _demo_book()


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
    feed = book_agent.build_today(_load_book(db, user))
    name, _ = _advisor_identity(user)
    feed["advisor_name"] = name
    return feed


@router.post("/refresh")
def refresh(db: Session = Depends(get_db),
            user: models.User = Depends(current_user)):
    """Re-run the batch over the book. Same shape as /today; busts the
    assessment cache so the next loads pick up fresh LLM verdicts."""
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
    msg = book_agent.draft_message(
        contact, body.trigger, channel=body.channel,
        user_name=name, user_role=role)
    return {"channel": body.channel, **msg}


@router.post("/ask")
def ask(body: AskIn, db: Session = Depends(get_db),
        user: models.User = Depends(current_user)):
    """The 'Ask your agent anything' bar + chip queries."""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(422, "query is required")
    return book_agent.ask_agent(_load_book(db, user), q)


@router.get("/relationship/{contact_id}")
def relationship(contact_id: str, db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)):
    """The relationship detail screen : health, the plain-language 'why', the
    relationship value, and a synthesized timeline. The drafted message is
    fetched separately via /draft so it can be refined independently."""
    contact = _find_contact(_load_book(db, user), contact_id=contact_id, name=None)
    if contact is None:
        raise HTTPException(404, "contact not found")
    return book_agent.relationship_detail(contact)
