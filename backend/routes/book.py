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

import hmac
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
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
    t_upd = time.monotonic() - t
    rel_agent._spine_prof_reset()
    t = time.monotonic()
    out = _book_from_spine_contacts(db, user, contacts, inter_index, update_index)
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
    book = _book_from_spine_contacts(db, user, [match], inter_index, update_index)
    return book[0] if book else None


def _book_from_spine_contacts(db, user, contacts, inter_index, update_index):
    """Inner loop of _book_from_spine, reusable for single-contact fast path."""
    now = datetime.now(timezone.utc)
    book = []
    for c in contacts:
        # `or []`, NOT `.get(c.id)`: a contact with no updates isn't in the index,
        # so .get returns None -> contact_summary reads that as "not prefetched"
        # and fires a per-contact fetch_activity_updates DB query (the N+1 that
        # made summary_loop ~10s for 80 contacts). [] = "prefetched, none".
        row = rel_agent.contact_summary(db, c, inter_index, update_index.get(c.id) or [])
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
            "stage": row.get("relationship_stage"),
            "interaction_history": row.get("next_step") or "",
            "raw_signals": signals,
            # Relationship-type tags (sales/hiring/investor/partner/follow_up)
            # for the Book filter pills + search.
            "tags": _book_tags(row.get("contact_types")),
        })
    return book


def _load_book(db: Session, user: models.User) -> list[dict]:
    """Real book from the spine; only DEMO users fall back to the demo roster
    (a real account with an empty spine gets an empty book, not fake clients)."""
    t0 = time.monotonic()
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
    name, role = _advisor_identity(user)

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
                from ..agents import drafting
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
                    contact, trigger, channel=channel, user_name=name, user_role=role)
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


@router.post("/ask")
def ask(body: AskIn, db: Session = Depends(get_db),
        user: models.User = Depends(current_user)):
    """The 'Ask your agent anything' bar + chip queries."""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(422, "query is required")
    t0 = time.monotonic()
    book = _load_book(db, user)
    t_load = time.monotonic() - t0
    _t = time.monotonic()
    res = book_agent.ask_agent(book, q)        # selection only: draft=null
    t_select = time.monotonic() - _t
    people = res.get("people") or []
    # Backfill each selected person with a real voice + thread draft via the ONE
    # shared composer. Resolve Contact ORMs ONCE (list_contacts is expensive),
    # then fan the drafts out concurrently. Cards still show drafts inline.
    _t = time.monotonic()
    orm_by_id = {str(c.id): c for c in rel_agent.list_contacts(db, user.id)}
    t_orm = time.monotonic() - _t
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
    # Progressive, not batch: draft only the TOP few inline (people are returned
    # ranked) so a "draft everyone" ask can't fire a dozen Claude calls at once
    # (the burst that throttles + stalls). Every card still carries contact_id,
    # so the rest draft on-demand the instant the host taps Draft (1 call each,
    # ~instant). Tunable via ASK_INLINE_DRAFTS.
    inline = max(0, int(os.environ.get("ASK_INLINE_DRAFTS", "6")))
    jobs, idxs = jobs[:inline], idxs[:inline]
    _t = time.monotonic()
    if jobs:
        from ..agents import drafting
        drafts = drafting.compose_batch(db, user.id, jobs, directive=q)
        for j, i in enumerate(idxs):
            d = drafts[j]
            if d and (d.get("body") or "").strip():
                people[i]["draft"] = d["body"]
    t_draft = time.monotonic() - _t
    res["people"] = people
    drafted = sum(1 for p in people if (p.get("draft") or "").strip())
    total = time.monotonic() - t0
    # Phase breakdown = the "why is /ask slow" log. The browser cuts the request
    # at ~100s, so if total approaches that the client sees "Couldn't reach the
    # server" even though we return 200 -> flag it loudly with where the time went.
    line = (f"POST /ask user={user.id} q={q!r} -> {len(people)} people "
            f"({drafted} drafted) in {total:.1f}s "
            f"[load={t_load:.1f} select={t_select:.1f} orm={t_orm:.1f} "
            f"draft={t_draft:.1f}, {len(jobs)} drafted]")
    _trace((">>> SLOW " if total > 60 else "") + line)
    return res


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
        from ..agents import drafting
        from concurrent.futures import ThreadPoolExecutor, as_completed
        wdb = SessionLocal()
        t0 = time.monotonic()
        try:
            wuser = wdb.query(models.User).get(user_id)
            events.put(("status", {"phase": "selecting"}))
            book = _load_book(wdb, wuser)
            res = book_agent.ask_agent(book, q)          # selection (Haiku, gated)
            people = res.get("people") or []
            orm_by_id = {str(c.id): c for c in rel_agent.list_contacts(wdb, user_id)}
            by_name = {(c.get("name") or "").strip().lower(): c for c in book}
            for p in people:
                bd = by_name.get((p.get("name") or "").strip().lower())
                if bd and bd.get("id"):
                    p["contact_id"] = bd["id"]
            # Show the ranked list NOW (drafts fill in next) -- first paint ~3s.
            events.put(("people", {"people": people, "answer": res.get("answer")}))
            # Draft the top few, emitting each as it lands. Build DB contexts
            # SERIALLY (session not thread-safe), then fan the pure-LLM calls out.
            inline = max(0, int(os.environ.get("ASK_INLINE_DRAFTS", "6")))
            name_, role_ = _advisor_identity(wuser)
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
                        user_name=name_, user_role=role_)
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
                                 "count": len(people)}))
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


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """Constant-time compare X-Admin-Token against ADMIN_TOKEN env (same gate as
    /admin/run-followups). Lets the scheduled GitHub Action fire the updates run
    without a user session."""
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/run-updates", status_code=202)
def run_updates_endpoint(user_id: Optional[int] = None, limit: int = 40,
                         _: None = Depends(_require_admin_token)):
    """Scheduled "what's new" sweep -> activity_update rows the Today feed reads.

    Resilient engine: Bright Data (scrapes profile job-changes + milestone posts
    on its own infra, delivered via /webhooks/brightdata) when configured, else
    account-safe Exa web search. Tiered by the vip ⭐ flag so paid scraping spend
    tracks the contacts that matter. `limit` caps contacts per run — pass a small
    value (e.g. ?limit=2) for a cheap validation batch. Runs in a background
    thread with its own session so the request returns immediately."""
    def _worker():
        from ..db import SessionLocal
        from ..agents.updates_engine import run_sweep
        db = SessionLocal()
        try:
            res = run_sweep(db, user_id=user_id, limit=max(1, min(limit, 200)))
            print(f"[updates] sweep {res}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[updates] run failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            db.close()
    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started", "scope": user_id if user_id is not None else "all",
            "limit": max(1, min(limit, 200))}


@router.get("/_updates-status")
def updates_status_endpoint(_: None = Depends(_require_admin_token)):
    """Cutover diagnostic: is Bright Data configured, what did the last sweep do
    (exa vs brightdata), and what fields did the last delivery parse. Token-gated,
    in-memory per replica — hit it right after a run to validate field-mapping."""
    from ..agents.updates_engine import status
    return status()


@router.post("/_updates-test")
def updates_test_endpoint(url: str, _: None = Depends(_require_admin_token)):
    """Fire a Bright Data scrape for ONE LinkedIn url (validation). Returns the
    immediate trigger outcome (status/response); the scraped data arrives async at
    /webhooks/brightdata — then GET /_updates-status to see last_delivery and
    validate the field mapping. Cheap: one record on the free credits."""
    from ..providers import brightdata
    ok = brightdata.trigger_updates([url])
    return {"triggered": ok, "last_trigger": brightdata.last_trigger()}


@router.get("/_status")
def book_status(_: None = Depends(_require_admin_token)):
    """At-a-glance health WITHOUT log-diving: request counts, recent errors / slow
    requests, Claude-call stats, and live rate-gate state (is the relationship
    layer throttling right now). Token-gated; in-memory + per-replica, so hit it a
    couple times for a fuller multi-replica picture.

        curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \\
             https://event.surpluslayer.com/api/book/_status | jq
    """
    from .. import metrics
    return metrics.snapshot()
