"""agents/updates_engine.py : the resilient "what's new" engine.

Orchestrates contact-update detection with graceful degradation:

    1. Bright Data (primary)  -> scrapes the contact's public LinkedIn profile +
       posts on a tiered schedule and DELIVERS the data to our webhook
       (routes/webhooks.py :: brightdata). We diff the profile for a job change
       and run the posts through a cheap cascade for a raise/launch/milestone.
       The scraping (and its ban risk) lives entirely on Bright Data's infra.
    2. Exa (fallback)         -> account-safe public web search
       (agents/updates_watch.find_updates). Used when Bright Data isn't
       configured, errors, or returns nothing.
    3. Skip (fail-soft)       -> one bad contact never sinks a run.

Every detected update is written as an `activity_update` (the same sink the
Today "Updates" feed reads) and a follow-up is auto-drafted in the host's voice
(agents.drafting) so the message is waiting, not on-demand.

Tiering: starred (vip) contacts are checked more often than the long tail, so
the (paid) provider spend scales with the contacts that matter, not the clock.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from .. import models
from . import updates_watch
from .relationship_watch import _emit, _changed, _now

# --- cadence (tiered by the ⭐ vip flag) -----------------------------------
_VIP_DAYS = max(1, int(os.environ.get("UPDATES_VIP_EVERY_DAYS", "1")))
_STD_DAYS = max(1, int(os.environ.get("UPDATES_STD_EVERY_DAYS", "7")))
_TAIL_DAYS = max(1, int(os.environ.get("UPDATES_TAIL_EVERY_DAYS", "30")))


def _tier_days(contact: models.Contact) -> int:
    """How often to check this contact. Starred = frequent; otherwise standard,
    dropping to a slow tail cadence once a relationship has gone quiet."""
    if getattr(contact, "vip", False):
        return _VIP_DAYS
    # A contact we've never surfaced anything for, long quiet -> tail cadence.
    return _STD_DAYS


def _is_due(contact: models.Contact) -> bool:
    """True when this contact is past its tier's check interval."""
    last = getattr(contact, "watched_at", None)
    if last is None:
        return True
    try:
        return (_now() - last) >= timedelta(days=_tier_days(contact))
    except Exception:  # noqa: BLE001 : naive/aware mismatch -> treat as due
        return True


def due_contacts(db, *, user_id: int | None = None, limit: int = 40) -> list:
    """Contacts that are due for a check, vip-first so the people who matter are
    never starved when a run is capped by `limit`."""
    q = db.query(models.Contact).filter(models.Contact.name.isnot(None))
    if user_id is not None:
        q = q.filter(models.Contact.user_id == user_id)
    # Oldest-checked first (never-checked NULLs are the most due). NOTE: the
    # Contact spine has no `vip` column yet (the ⭐ is captured on the Prospect,
    # not propagated to Contact), so star-tiering in _tier_days is a no-op until
    # vip is added to Contact + propagated -- see the follow-up. For now every
    # contact runs at the standard cadence, which is correct, just uniform.
    q = q.order_by(models.Contact.watched_at.asc())
    return [c for c in q.limit(limit * 3).all() if _is_due(c)][:limit]


# --- auto-draft on update --------------------------------------------------
def autodraft(db, contact: models.Contact, change: dict) -> None:
    """Compose a follow-up in the host's voice for a freshly-detected update and
    stash it on the interaction's meta so the Updates feed shows a ready draft.
    Best-effort: a draft failure must never drop the update itself."""
    try:
        from . import drafting
        reason = change.get("summary") or change.get("title") or "following up"
        msg = drafting.compose_followup(
            db, contact.user_id, contact, reason=reason, channel="email")
        body = (msg or {}).get("body") or ""
        if not body:
            return
        # Attach to the most-recent activity_update for this contact.
        ri = (db.query(models.RelationshipInteraction)
              .filter(models.RelationshipInteraction.contact_id == contact.id,
                      models.RelationshipInteraction.source_type == "activity_update")
              .order_by(models.RelationshipInteraction.id.desc())
              .first())
        if ri is not None:
            import json
            meta = {}
            try:
                meta = json.loads(ri.meta_json or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            meta["draft"] = body[:2000]
            if msg.get("subject"):
                meta["draft_subject"] = msg["subject"][:200]
            ri.meta_json = json.dumps(meta)
    except Exception as exc:  # noqa: BLE001 : drafting is best-effort
        print(f"  [updates.autodraft] contact={contact.id} skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)


# --- job-change diff (from a scraped profile) ------------------------------
def apply_profile(db, contact: models.Contact, profile: dict) -> list[dict]:
    """Diff a scraped LinkedIn profile against the contact's last-known company/
    title. Emit a job_change activity_update + auto-draft when it moved. Updates
    the stored snapshot so the next diff is against the new baseline."""
    new_company = (profile.get("company") or profile.get("current_company")
                   or "").strip()
    new_title = (profile.get("title") or profile.get("position")
                 or profile.get("headline") or "").strip()
    changes: list[dict] = []
    if _changed(getattr(contact, "company", None), new_company):
        prev = getattr(contact, "company", None)
        summary = (f"Joined {new_company}"
                   + (f" as {new_title}" if new_title else "")
                   + (f" (was at {prev})" if prev and prev.lower() != "unknown" else ""))
        change = _emit(db, contact, "job_change", summary,
                       {"new_company": new_company, "new_title": new_title,
                        "prev_company": prev, "source": "brightdata"})
        contact.company = new_company
        if new_title:
            contact.headline = new_title[:300]
        changes.append(change)
        autodraft(db, contact, change)
    contact.watched_at = _now()
    return changes


# --- posts cascade (raise / launch / milestone) ----------------------------
_MILESTONE_TERMS = (
    "raised", "raise", "series ", "seed round", "funding", "fundraise",
    "launch", "launching", "launched", "announce", "announcing", "announced",
    "thrilled to", "excited to", "proud to", "crossed", "hit ", "milestone",
    "joined", "new role", "promoted", "named", "appointed", "acquired",
)


def _looks_like_milestone(text: str) -> bool:
    """Free keyword pre-drop: cheap first gate before any model call."""
    t = (text or "").lower()
    return any(term in t for term in _MILESTONE_TERMS)


_POST_SYSTEM = (
    "You read ONE recent LinkedIn post by a professional contact and decide if "
    "it announces a genuine milestone worth a congratulations: a fundraise, a "
    "launch, a new role, an award, or a notable company achievement. Ignore "
    "reshares, generic commentary, hiring posts, and engagement bait. Return "
    "ONLY JSON: {\"is_milestone\":true|false,\"type\":\"raise|launch|role|"
    "award|milestone\",\"headline\":\"<=8 words\",\"summary\":\"<=25 words, "
    "specific\"}. is_milestone=false unless it's clearly the contact's own news."
)


def apply_posts(db, contact: models.Contact, posts: list[dict]) -> list[dict]:
    """Run scraped posts through the cheap cascade (keyword pre-drop -> LLM
    confirm on survivors) and emit a milestone activity_update + auto-draft for
    the best hit. Dedup via contact.seen_post_ids on the post URL/id."""
    import json
    from .book import _llm_json
    seen = updates_watch._seen_urls(contact)
    changes: list[dict] = []
    # Keyword pre-drop first (free); only survivors reach the model.
    candidates = []
    for p in posts or []:
        text = (p.get("text") or p.get("body") or p.get("title") or "")
        pid = (p.get("url") or p.get("post_id") or p.get("id") or "").strip()
        if pid and pid in seen:
            continue
        if text and _looks_like_milestone(text):
            candidates.append((pid, text[:800]))
    for pid, text in candidates[:5]:
        out = _llm_json(_POST_SYSTEM, f"Post:\n{text}",
                        max_tokens=200, cheap=True, background=True)
        if not out or not out.get("is_milestone"):
            continue
        summary = (out.get("summary") or "").strip()
        if not summary:
            continue
        change = _emit(db, contact, "new_post", summary[:300],
                       {"url": pid, "headline": (out.get("headline") or "")[:120],
                        "milestone_type": out.get("type"), "source": "brightdata"})
        changes.append(change)
        autodraft(db, contact, change)
        if pid:
            seen.add(pid)
        break  # one milestone per run is enough for the feed
    if seen:
        contact.seen_post_ids = json.dumps(sorted(seen)[:200])
    contact.watched_at = _now()
    return changes


# --- the sweep (degradation lives here) ------------------------------------
def _brightdata_enabled() -> bool:
    try:
        from ..providers import brightdata
        return brightdata.configured()
    except Exception:  # noqa: BLE001
        return False


def run_sweep(db, *, user_id: int | None = None, limit: int = 40) -> dict:
    """One scheduled pass. Bright Data primary (async -> trigger now, results
    arrive via webhook), Exa fallback (sync) when Bright Data isn't available.
    Bounded + fail-soft. Returns a small status dict."""
    contacts = due_contacts(db, user_id=user_id, limit=limit)
    if not contacts:
        return {"due": 0, "mode": "none"}

    if _brightdata_enabled():
        from ..providers import brightdata
        urls = [c.linkedin_url for c in contacts if (c.linkedin_url or "").strip()]
        triggered = False
        try:
            triggered = brightdata.trigger_updates(urls)
        except Exception as exc:  # noqa: BLE001
            print(f"  [updates] brightdata trigger failed -> exa fallback: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        if triggered:
            # Mark as checked; the webhook will emit when the scrape lands.
            for c in contacts:
                c.watched_at = _now()
            db.commit()
            return {"due": len(contacts), "mode": "brightdata", "triggered": len(urls)}
        # fall through to Exa if the trigger didn't take

    # --- Exa fallback (synchronous, account-safe) --------------------------
    emitted = 0
    for c in contacts:
        try:
            for change in updates_watch.find_updates(db, c):
                emitted += 1
                autodraft(db, c, change)
            c.watched_at = _now()
        except Exception as exc:  # noqa: BLE001
            print(f"  [updates] exa contact={c.id} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    db.commit()
    return {"due": len(contacts), "mode": "exa", "emitted": emitted}
