"""agents/relationship_watch.py : keep a user's CRM (Contact spine) fresh.

There is NO push/webhook for a tracked person's own LinkedIn activity — Unipile
only pushes the *connected account's* messages, invite-accepts and account
status (see routes/webhooks.py). So to surface "X changed jobs" / "Y posted",
we POLL: on a schedule, re-fetch each Contact's live LinkedIn via Unipile and
diff it against the last snapshot we stored on the Contact row.

Each real change becomes an append-only RelationshipInteraction with
source_type="activity_update", so it flows straight into the existing
relationship timeline (agents/relationships.py::build_timeline already renders
stored interactions) and the new "what's new" feed
(routes/relationships.py::GET /updates).

Design rules
------------
- FIRST poll seeds the baseline SILENTLY (watched_at was NULL). We never emit
  "changed jobs" for state we never recorded, and we mark every existing post
  as already-seen so we don't flood the feed with their back-catalogue.
- Subsequent polls diff: title/company change -> job_change; headline-only
  change -> profile_update; each previously-unseen post id -> new_post.
- Read-only + best-effort: any fetch error is recorded on Contact.watch_error
  and the contact is skipped, never crashing the sweep. Respects the provider's
  dry-run gate (UNIPILE_DRY_RUN) — under dry-run the provider returns canned
  data, which still exercises the full diff/emit pipeline.
"""
from __future__ import annotations
import json
from datetime import datetime

from sqlalchemy.orm import Session

from .. import models

# Human labels for the timeline item title (summary carries the specifics).
_TITLES = {
    "job_change": "Changed roles",
    "profile_update": "Updated profile",
    "new_post": "New LinkedIn post",
}


def _now() -> datetime:
    # Naive UTC, matching the rest of the schema's _utcnow convention.
    return datetime.utcnow()


def _norm(s: str | None) -> str:
    return (s or "").strip()


def _changed(old: str | None, new: str) -> bool:
    """True when `new` is non-empty and differs (case-insensitively) from old.
    A fetch that returns nothing for a field never clears a known value."""
    return bool(new) and _norm(old).lower() != new.strip().lower()


def _company_from_position(position: str) -> str:
    """Parse the company out of a 'Title @ Company' position string."""
    if " @ " in position:
        return position.split(" @ ", 1)[1].strip()
    return ""


def _emit(db: Session, contact: models.Contact, kind: str,
          summary: str, meta: dict) -> dict:
    """Append an activity_update RelationshipInteraction for one change and
    return a compact dict for the job's response/feed."""
    ri = models.RelationshipInteraction(
        actor_user_id=contact.user_id,
        contact_id=contact.id,
        company_domain=contact.company_domain,
        source_type="activity_update",
        interaction_type=kind,
        direction="none",
        occurred_at=_now(),
        title=_TITLES.get(kind, "Update"),
        summary=summary[:1000],
        meta_json=json.dumps(meta),
        visibility="private",
    )
    db.add(ri)
    # Flush so the row gets an id and is queryable IMMEDIATELY -- the session is
    # autoflush=False, so without this the row stays pending until the caller's
    # commit and autodraft (which runs in between) can't find it to attach a draft.
    db.flush()
    change = {
        "ri_id": ri.id,
        "contact_id": contact.id,
        "name": contact.name,
        "type": kind,
        "title": ri.title,
        "summary": ri.summary,
    }
    # Auto-draft a follow-up for EVERY emitted update, no matter which watcher
    # found it (Bright Data, Exa, or the Unipile CRM refresh) -- so the Updates
    # feed always has a ready message. Lazy import avoids a circular import;
    # best-effort so a draft failure never drops the update itself. Idempotent
    # (autodraft skips a row that already has a draft).
    try:
        from .updates_engine import autodraft
        autodraft(db, contact, change)
    except Exception as exc:  # noqa: BLE001
        print(f"  [_emit.autodraft] contact={contact.id} skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
    return change


def refresh_contact(db: Session, contact: models.Contact, provider) -> list[dict]:
    """Re-fetch one contact's LinkedIn, diff vs snapshot, emit changes.

    Returns the list of change dicts (empty on first poll / no change / error).
    Commits its own writes so a long sweep makes incremental progress.
    """
    if not _norm(contact.linkedin_url):
        return []

    first_poll = contact.watched_at is None
    try:
        profile = provider.fetch_profile(contact.linkedin_url) or {}
        # The posts subpath is keyed by the internal provider_id (the public
        # handle 422s), which fetch_profile surfaces. No provider_id -> skip
        # posts (still diff the profile fields we did get).
        pid = (profile.get("provider_id") or "").strip()
        posts = provider.fetch_recent_posts_detailed(pid) if pid else []
    except Exception as exc:  # noqa: BLE001
        contact.watch_error = f"{type(exc).__name__}: {exc}"[:300]
        db.commit()
        return []

    new_headline = _norm(profile.get("headline"))
    new_title = _norm(profile.get("position"))
    new_company = _company_from_position(new_title)

    changes: list[dict] = []

    # --- profile / job changes (only once we have a baseline) ---------------
    if not first_poll:
        if _changed(contact.title, new_title):
            was = f" (was {contact.title})" if contact.title else ""
            changes.append(_emit(
                db, contact, "job_change", f"Now {new_title}{was}",
                {"old_title": contact.title, "new_title": new_title,
                 "old_company": contact.company, "new_company": new_company},
            ))
        elif _changed(contact.headline, new_headline):
            changes.append(_emit(
                db, contact, "profile_update",
                f"Updated headline: {new_headline}",
                {"old_headline": contact.headline,
                 "new_headline": new_headline},
            ))

    # --- new posts ----------------------------------------------------------
    try:
        seen = set(json.loads(contact.seen_post_ids or "[]"))
    except (ValueError, TypeError):
        seen = set()
    for p in posts:
        pid = str(p.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        if not first_poll:
            snippet = _norm(p.get("text")) or "Posted on LinkedIn"
            changes.append(_emit(
                db, contact, "new_post", snippet[:300],
                {"post_id": pid, "date": p.get("date", "")},
            ))

    # --- persist the fresh snapshot ----------------------------------------
    if new_headline:
        contact.headline = new_headline[:300]
    if new_title:
        contact.title = new_title[:200]
    if new_company:
        contact.company = new_company[:120]
    contact.seen_post_ids = json.dumps(sorted(seen))
    contact.watched_at = _now()
    contact.watch_error = None
    db.commit()
    return changes


def refresh_user_crm(db: Session, user_id: int, provider,
                     limit: int | None = None) -> dict:
    """Poll every LinkedIn-resolvable Contact in one user's CRM, oldest-checked
    first (so a capped sweep makes round-robin progress). Returns a summary
    {polled, changes, items}."""
    contacts = (
        db.query(models.Contact)
        .filter(models.Contact.user_id == user_id)
        .all()
    )
    # LinkedIn-resolvable only, oldest-watched first (NULL == never -> front).
    contacts = [c for c in contacts if _norm(c.linkedin_url)]
    contacts.sort(key=lambda c: (c.watched_at or datetime.min))
    if limit:
        contacts = contacts[:limit]

    items: list[dict] = []
    for c in contacts:
        items.extend(refresh_contact(db, c, provider))
    return {"polled": len(contacts), "changes": len(items), "items": items}
