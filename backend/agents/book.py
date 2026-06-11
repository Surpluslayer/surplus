"""
agents/book.py : the "Your book today" relationship engine for the advisor
surface (BookApp).

Four operations, mirroring the product spec:

  1. score_health(contact)      -> relationship health + whether they need
                                   outreach. Drives the "Needs outreach" list
                                   and the status dot color.
  2. detect_update(contact)     -> turns raw signals into a noteworthy
                                   "Updates" feed item (and whether it earns a
                                   "Draft" via outreach_trigger).
  3. draft_message(...)         -> the note behind every "Draft" tap. Handles
                                   both the warm (congratulate) and cold
                                   (re-engage) cases from one prompt.
  4. ask_agent(book, query)     -> the freeform "Ask your agent anything" bar
                                   and the chip queries ("Who's cooling?").

Every operation calls Claude when ANTHROPIC_API_KEY is set, and falls back to a
deterministic heuristic otherwise — so the surface renders end-to-end (and the
demo book looks right) with no key configured. Prompts 1 & 2 are batch-shaped
(run across the whole book, cache the result so Today loads instantly); prompt
3 fires on tap; prompt 4 is interactive.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Optional


# ─── LLM plumbing (graceful, key-optional) ───────────────────────────────────

def _anthropic_available() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def _llm_json(system: str, user: str, *, max_tokens: int = 700) -> Optional[dict]:
    """Call Claude in JSON mode and parse the first JSON object out of the reply.

    Returns None on any failure (no key, SDK missing, rate-limit, unparseable) so
    every caller can fall back to its deterministic path. Never raises."""
    if not _anthropic_available():
        return None
    try:
        from . import llm  # reuse the configured client + model constant
        resp = llm._client().messages.create(
            model=llm.MODEL,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        # Be forgiving: the model occasionally wraps JSON in prose/fences.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        return json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001 : LLM is best-effort, fall back silently
        return None


# ─── 1. Relationship health + outreach scoring ───────────────────────────────

_HEALTH_SYSTEM = (
    "You score the health of a professional relationship for a wealth advisor / "
    "lawyer whose income depends on long-term client trust. Classify health and "
    "whether they need outreach. A relationship is overdue when days_since "
    "exceeds the expected cadence, weighted by tier (key clients tolerate less "
    "silence). A review coming due or overdue always warrants outreach. Return "
    "ONLY JSON, no prose: {\"status\":\"active|warm|cooling|dormant\","
    "\"needs_outreach\":true|false,\"reason\":\"<=6 words\",\"priority\":1-100}"
)


def score_health(contact: dict) -> dict:
    """Health + outreach verdict for one contact. {status, needs_outreach,
    reason, priority}."""
    user = (
        "Contact:\n"
        f"- Name: {contact.get('name')} | Title: {contact.get('title')} @ "
        f"{contact.get('firm')} | Tier: {contact.get('tier')}\n"
        f"- Last meaningful contact: {contact.get('last_contact_date')} "
        f"({contact.get('days_since')} days ago)\n"
        f"- Expected cadence for this tier: {contact.get('cadence_days')} days\n"
        f"- Review cycle: {contact.get('review_cadence')} | Next review due: "
        f"{contact.get('next_review_date')}\n"
        f"- Recent interactions: {contact.get('interaction_history')}\n"
    )
    out = _llm_json(_HEALTH_SYSTEM, user, max_tokens=300)
    if out and "status" in out and "needs_outreach" in out:
        out.setdefault("reason", "")
        out.setdefault("priority", 50)
        return out
    return _score_health_heuristic(contact)


def _score_health_heuristic(contact: dict) -> dict:
    """Deterministic cadence math used when no LLM is configured."""
    days = int(contact.get("days_since") or 0)
    cadence = int(contact.get("cadence_days") or 90)
    review_due = bool(contact.get("review_due"))
    # Tier weighting: key clients tolerate less silence (lower effective cadence).
    tier = (contact.get("tier") or "").lower()
    weight = {"key": 0.6, "a": 0.7, "core": 0.8}.get(tier, 1.0)
    eff = max(1, int(cadence * weight))
    ratio = days / eff if eff else 0

    if ratio >= 2 or days >= 90:
        status = "dormant"
    elif ratio >= 1:
        status = "cooling"
    elif ratio >= 0.6:
        status = "warm"
    else:
        status = "active"

    # A due/overdue review pulls the relationship down to at least "cooling" —
    # it reads (and dots) as needing attention everywhere, even if cadence
    # alone would call it warm/active.
    if review_due and status in ("active", "warm"):
        status = "cooling"

    needs = review_due or status in ("cooling", "dormant")
    if review_due and days > 0:
        reason = f"Quiet {days} days · review due"
    elif review_due:
        reason = "Review due"
    else:
        reason = f"Quiet {days} days"

    # Priority: overdue-ness + tier + review pressure, clamped 1..100.
    priority = min(100, int(ratio * 45) + (30 if review_due else 0)
                   + {"key": 20, "a": 12, "core": 6}.get(tier, 0))
    priority = max(1, priority)
    return {"status": status, "needs_outreach": needs,
            "reason": reason, "priority": priority}


# ─── 2. Update detection (prospecting) ───────────────────────────────────────

_UPDATE_SYSTEM = (
    "You monitor a relationship book for events worth a personal note. Given raw "
    "signals about one contact, decide if there is a noteworthy update and "
    "whether it is a good reason to reach out now. Noteworthy types: job_change, "
    "promotion, liquidity_event, fundraise, award, relocation, company_news. "
    "Ignore routine posts, reshares, and stale items (> 30 days old) unless "
    "high-significance (e.g. liquidity event). Return ONLY JSON, no prose: "
    "{\"has_update\":true|false,\"type\":\"<type>\",\"headline\":\"<=5 words\","
    "\"detected_at\":\"<ISO date>\",\"outreach_trigger\":true|false,"
    "\"significance\":\"low|medium|high\"}"
)


def detect_update(contact: dict) -> Optional[dict]:
    """Decide if a contact has a noteworthy update. Returns the update dict, or
    None when there's nothing worth a note."""
    signals = contact.get("raw_signals")
    if not signals:
        return None
    user = (
        f"Contact: {contact.get('name')}, {contact.get('title')} @ "
        f"{contact.get('firm')}\n"
        f"Signals (with detected dates): {signals}\n"
    )
    out = _llm_json(_UPDATE_SYSTEM, user, max_tokens=300)
    if out is not None:
        if not out.get("has_update"):
            return None
        out.setdefault("type", "company_news")
        out.setdefault("headline", "")
        out.setdefault("detected_at", _iso_today())
        out.setdefault("outreach_trigger", True)
        out.setdefault("significance", "medium")
        return out
    return _detect_update_heuristic(contact)


def _detect_update_heuristic(contact: dict) -> Optional[dict]:
    """Pass the seeded/structured signal through when no LLM is configured."""
    sig = contact.get("raw_signals")
    if isinstance(sig, dict):
        if not sig.get("headline"):
            return None
        return {
            "has_update": True,
            "type": sig.get("type", "company_news"),
            "headline": sig.get("headline", ""),
            "detected_at": sig.get("detected_at", _iso_today()),
            "outreach_trigger": bool(sig.get("outreach_trigger", True)),
            "significance": sig.get("significance", "medium"),
        }
    return None


# ─── Assessment cache ────────────────────────────────────────────────────────
#
# Page loads must never wait on the model (spec: prompts 1 & 2 are batch-shaped;
# render from cache). assess() returns instantly — the cached LLM verdict when
# fresh, the deterministic heuristic otherwise — and warms the LLM verdict on a
# background thread. The fingerprint folds in everything the prompts read, so a
# contact's verdict refreshes naturally when their state (or the day) changes.

_ASSESS_TTL = 6 * 3600  # seconds; days_since shifting daily re-keys anyway
_assess_cache: dict[str, tuple[float, dict, Optional[dict]]] = {}
_assess_inflight: set[str] = set()
_assess_lock = threading.Lock()


def _assess_key(contact: dict) -> str:
    raw = json.dumps(
        [contact.get("id"), contact.get("name"), contact.get("days_since"),
         contact.get("stage"), contact.get("review_due"), contact.get("tier"),
         contact.get("raw_signals")],
        sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


def _assess_llm(contact: dict, key: str) -> None:
    try:
        h = score_health(contact)
        u = detect_update(contact)
        with _assess_lock:
            _assess_cache[key] = (time.time(), h, u)
    finally:
        with _assess_lock:
            _assess_inflight.discard(key)


def assess(contact: dict) -> tuple[dict, Optional[dict]]:
    """One cached (health, update) verdict per contact. Never blocks on the
    model: cold-cache requests get the heuristic and kick off the LLM refresh
    in the background."""
    key = _assess_key(contact)
    now = time.time()
    spawn = False
    with _assess_lock:
        hit = _assess_cache.get(key)
        if hit and now - hit[0] < _ASSESS_TTL:
            return hit[1], hit[2]
        if _anthropic_available() and key not in _assess_inflight:
            _assess_inflight.add(key)
            spawn = True
    if spawn:
        threading.Thread(target=_assess_llm, args=(contact, key),
                         daemon=True).start()
    h = _score_health_heuristic(contact)
    u = _detect_update_heuristic(contact)
    if not _anthropic_available():
        # The heuristic IS the final verdict with no key — cache it so repeat
        # loads skip even the recompute.
        with _assess_lock:
            _assess_cache[key] = (now, h, u)
    return h, u


def invalidate_assessments() -> None:
    """Drop every cached verdict (the /refresh endpoint's 'Refresh now')."""
    with _assess_lock:
        _assess_cache.clear()
    with _draft_lock:
        _draft_cache.clear()


# ─── Draft cache + pre-drafting ──────────────────────────────────────────────
#
# Drafting is the one model call that's supposed to be live — but waiting 3-8s
# on every relationship open, including re-opens of the same person, is wasted.
# Cache by (contact, trigger, channel) with the same TTL as assessments, and
# let build_today kick off background pre-drafts for the people the user is
# about to be told to contact, so the panel is usually instant by the time
# they tap.

_draft_cache: dict[str, tuple[float, dict]] = {}
_draft_inflight: set[str] = set()
_draft_lock = threading.Lock()


def _draft_key(contact: dict, trigger: str, channel: str) -> str:
    raw = json.dumps([contact.get("id"), contact.get("name"), trigger, channel,
                      contact.get("interaction_history")],
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


def draft_message_cached(contact: dict, trigger: str, *, channel: str = "email",
                         user_name: str = "your advisor",
                         user_role: str = "wealth advisor") -> dict:
    """draft_message with a TTL cache: a re-open of the same person + trigger
    returns instantly instead of re-paying the model call."""
    key = _draft_key(contact, trigger, channel)
    now = time.time()
    with _draft_lock:
        hit = _draft_cache.get(key)
        if hit and now - hit[0] < _ASSESS_TTL:
            return hit[1]
    msg = draft_message(contact, trigger, channel=channel,
                        user_name=user_name, user_role=user_role)
    with _draft_lock:
        _draft_cache[key] = (now, msg)
    return msg


def predraft(contacts_with_triggers: list[tuple[dict, str]],
             *, user_name: str = "your advisor",
             user_role: str = "wealth advisor") -> None:
    """Warm the draft cache in the background for (contact, trigger) pairs the
    Today feed is about to surface. Fire-and-forget; never blocks a request."""
    if not _anthropic_available():
        return  # the heuristic draft is instant anyway — nothing to warm
    pending = []
    with _draft_lock:
        for c, trig in contacts_with_triggers:
            key = _draft_key(c, trig, "email")
            if key in _draft_cache or key in _draft_inflight:
                continue
            _draft_inflight.add(key)
            pending.append((c, trig, key))

    def _run(c, trig, key):
        try:
            draft_message_cached(c, trig, user_name=user_name, user_role=user_role)
        finally:
            with _draft_lock:
                _draft_inflight.discard(key)

    for c, trig, key in pending:
        threading.Thread(target=_run, args=(c, trig, key), daemon=True).start()


# ─── 3. Draft a message ──────────────────────────────────────────────────────

def _draft_system(user_name: str, user_role: str) -> str:
    return (
        f"Write a short outreach message in {user_name}'s voice. {user_name} is a "
        f"{user_role}; tone is warm, specific, and never salesy — the kind of note "
        "a trusted advisor sends, not a pitch. Rules: 2-4 sentences. No "
        "subject-line cliches, no 'I hope this finds you well.' Reference one "
        "concrete, true detail from the history if available. For a "
        "congratulation: lead with the news, no ask. For re-engagement: gentle, "
        "offer something (a review, a catch-up), not a demand. If channel is "
        "email, also return a 3-5 word subject. Return ONLY JSON: "
        "{\"subject\":\"<email only, else null>\",\"body\":\"<the message>\"}"
    )


def draft_message(contact: dict, trigger: str, *, channel: str = "email",
                  user_name: str = "your advisor",
                  user_role: str = "wealth advisor") -> dict:
    """The note behind a 'Draft' tap. Returns {subject, body}."""
    user = (
        f"To: {contact.get('name')}, {contact.get('title')} @ {contact.get('firm')}\n"
        f"Reason for reaching out: {trigger}\n"
        f"Shared history to draw on: {contact.get('interaction_history')}\n"
        f"Channel: {channel}\n"
    )
    out = _llm_json(_draft_system(user_name, user_role), user, max_tokens=500)
    if out and out.get("body"):
        if channel != "email":
            out["subject"] = None
        return {"subject": out.get("subject"), "body": out["body"]}
    return _draft_message_heuristic(contact, trigger, channel, user_name)


def _draft_message_heuristic(contact: dict, trigger: str, channel: str,
                             user_name: str) -> dict:
    name = (contact.get("name") or "there").split()[0]
    t = (trigger or "").lower()
    congrats = any(k in t for k in (
        "promot", "rais", "fund", "liquid", "award", "new role", "joined"))
    if congrats:
        body = (f"Hi {name}, just saw the news — {trigger.rstrip('.')}. "
                "Genuinely happy for you; you've earned it. Would love to hear "
                "how it came together when you have a minute.")
        subject = "Congratulations"
    else:
        body = (f"Hi {name}, it's been a little while and you've been on my mind. "
                "No agenda — I'd love to catch up and make sure everything's still "
                "lined up on your end. Happy to put time on the calendar whenever "
                "suits you.")
        subject = "Catching up"
    return {"subject": subject if channel == "email" else None, "body": body}


# ─── 4. The agent ask bar ────────────────────────────────────────────────────

_ASK_SYSTEM = (
    "You are the relationship assistant inside Surplus. You answer questions "
    "about the user's book by reasoning over their contacts, and you draft "
    "messages on request. Answer concisely. When the question implies a list "
    "(who's cooling, reviews due, who to follow up with), return the matching "
    "people ranked by priority. When the user asks you to draft or 'ping', "
    "produce the message(s) directly. Never invent interactions or facts not "
    "present in the book data. Return ONLY JSON: {\"answer\":\"<one or two "
    "sentences>\",\"people\":[{\"name\":\"...\",\"reason\":\"...\","
    "\"draft\":\"<null or a message>\"}]}"
)


def ask_agent(book: list[dict], query: str) -> dict:
    """The freeform ask bar + chip queries. {answer, people}."""
    user = (
        "The user's book (scored contacts with history):\n"
        + json.dumps(book, default=str)
        + f"\n\nUser's question: {query}\n"
    )
    out = _llm_json(_ASK_SYSTEM, user, max_tokens=900)
    if out and "answer" in out:
        out.setdefault("people", [])
        return out
    return _ask_agent_heuristic(book, query)


def _ask_agent_heuristic(book: list[dict], query: str) -> dict:
    """Keyword routing over the scored book when no LLM is configured."""
    q = (query or "").lower()
    scored = [{**c, **score_health(c)} for c in book]

    def _people(items):
        return [{"name": c.get("name"), "reason": c.get("reason"),
                 "draft": None} for c in items]

    if any(k in q for k in ("review", "due")):
        hits = sorted([c for c in scored if c.get("review_due")],
                      key=lambda c: -c["priority"])
        return {"answer": f"{len(hits)} client(s) have a review due or overdue.",
                "people": _people(hits)}
    if any(k in q for k in ("cool", "cold", "dormant", "quiet", "follow", "outreach")):
        hits = sorted([c for c in scored if c["needs_outreach"]],
                      key=lambda c: -c["priority"])
        return {"answer": f"{len(hits)} relationship(s) are cooling or overdue "
                          "for a touch.",
                "people": _people(hits)}
    # Default: surface the highest-priority handful.
    hits = sorted(scored, key=lambda c: -c["priority"])[:5]
    return {"answer": "Here are the people at the top of your book right now.",
            "people": _people(hits)}


# ─── Today feed assembler (batch over the book) ──────────────────────────────

def build_today(book: list[dict]) -> dict:
    """Run detection + scoring across the whole book and assemble the Today
    feed: time-ordered Updates and priority-ranked Needs-outreach."""
    assessed = [(c, *assess(c)) for c in book]

    updates = []
    for c, _h, u in assessed:
        if not u:
            continue
        updates.append({
            "name": c.get("name"),
            "vip": bool(c.get("vip")),
            "headline": u.get("headline"),
            "detected_at": u.get("detected_at"),
            "type": u.get("type"),
            "significance": u.get("significance"),
            "can_draft": bool(u.get("outreach_trigger")),
            "trigger": u.get("headline"),
            "contact_id": c.get("id"),
        })
    updates.sort(key=lambda x: x.get("detected_at") or "", reverse=True)

    needs = []
    for c, h, _u in assessed:
        if not h.get("needs_outreach"):
            continue
        needs.append({
            "name": c.get("name"),
            "vip": bool(c.get("vip")),
            "reason": h.get("reason"),
            "status": h.get("status"),
            "priority": h.get("priority"),
            "trigger": h.get("reason"),
            "contact_id": c.get("id"),
        })
    needs.sort(key=lambda x: -(x.get("priority") or 0))
    return {
        "date": _iso_today(),
        "updates": updates,
        "needs_outreach": needs,
        # Full roster for the Book screen (every contact scored, richest-first
        # by who needs attention). Today renders from updates/needs; Book and
        # the relationship detail render from this.
        "roster": build_roster(book),
    }


# ─── Roster (the "Your book" screen) ─────────────────────────────────────────

def build_roster(book: list[dict]) -> list[dict]:
    """Score every contact and return a single attention-ranked roster. Powers
    the Book screen's list + filter pills (All / Starred / Cooling / Prospects)
    and seeds the relationship detail. No LLM call beyond the per-contact score
    (cached upstream in production)."""
    rows = []
    for c in book:
        h, upd = assess(c)
        rows.append({
            "contact_id": c.get("id"),
            "name": c.get("name"),
            "vip": bool(c.get("vip")),
            "title": c.get("title") or "",
            "firm": c.get("firm") or "",
            "met_at": c.get("met_at") or "",
            "days_since": int(c.get("days_since") or 0),
            "status": h.get("status"),
            "reason": h.get("reason"),
            "review_due": bool(c.get("review_due")),
            "needs_outreach": bool(h.get("needs_outreach")),
            "priority": h.get("priority"),
            "is_prospect": bool(c.get("is_prospect")),
            "stage": c.get("stage"),
            "value": c.get("value") or "",
            "has_update": upd is not None,
            "headline": (upd or {}).get("headline"),
        })
    # New prospects float to the top, then by attention (priority) desc.
    rows.sort(key=lambda r: (not r["is_prospect"], -(r.get("priority") or 0)))
    return rows


def relationship_detail(contact: dict) -> dict:
    """The relationship screen: health, a plain-language 'why', the timeline,
    and the relationship value line. The drafted message is fetched separately
    (draft_message) on open so it can be refined independently."""
    h, _ = assess(contact)
    status = h.get("status")
    days = int(contact.get("days_since") or 0)
    return {
        "contact_id": contact.get("id"),
        "name": contact.get("name"),
        "vip": bool(contact.get("vip")),
        "title": contact.get("title") or "",
        "firm": contact.get("firm") or "",
        "met_at": contact.get("met_at") or "",
        "status": status,
        "days_since": days,
        "reason": h.get("reason"),
        "review_due": bool(contact.get("review_due")),
        "value": contact.get("value") or "",
        "why": _why_text(contact, status, days),
        "timeline": _timeline(contact, status, days),
    }


def _why_text(contact: dict, status: str, days: int) -> str:
    """A short, true reasoning line for the detail header — built from the
    contact's own fields, never invented."""
    name = (contact.get("name") or "They").split(" ")[0]
    review = bool(contact.get("review_due"))
    tier = (contact.get("tier") or "").lower()
    big = tier in ("key", "a")
    if status in ("cooling", "dormant"):
        bits = [f"It's been {days} days since you last spoke"]
        if review:
            bits.append("their review is overdue")
        if big:
            bits.append(f"and {name} is one of your larger relationships")
        lead = ", ".join(bits) + "."
        return (lead + " A personal note this week is worth more than another "
                "quarter of silence.")
    if status == "warm":
        return (f"You spoke {days} days ago — still warm, but the kind of "
                "relationship that fades without a light touch.")
    if status == "active" and contact.get("raw_signals"):
        return (f"{name} is active and there's fresh news worth a note — a good "
                "moment to reach out while it's top of mind.")
    return f"You're in good standing with {name}. No action needed today."


def _timeline(contact: dict, status: str, days: int) -> list[dict]:
    """Synthesize an honest timeline from the fields we have: the last touch
    (flagged when it's gone quiet), the background note, and where you met."""
    items = []
    if days > 0:
        items.append({
            "t": ("Sent a note · no reply" if status in ("cooling", "dormant")
                  else "Last spoke"),
            "d": f"{days} days ago",
            "warn": status in ("cooling", "dormant"),
        })
    hist = (contact.get("interaction_history") or "").strip()
    if hist:
        items.append({"t": hist, "d": "Background", "warn": False})
    met = contact.get("met_at")
    if met:
        items.append({"t": f"Met at {met}", "d": "", "warn": False})
    return items


def _iso_today() -> str:
    return date.today().isoformat()
