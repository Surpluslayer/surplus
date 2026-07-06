"""
accounts_read.py : read-model assembly for the account layer (Accounts tab).

Portrayal only (docs/accounts-architecture.md §4) — this module READS the
account tables (Company / CompanyOverlay / AccountMembership / Account /
RelationshipInteraction) and assembles the owner's view of one account. It
never resolves or links companies (that is company_resolve.py's job) and it
never writes global Company rows: the viewer's corrections come in through
their CompanyOverlay, applied merge-on-read.

Level discipline: everything here is the OWNER's own view of their own graph
(owner_type="user"), so full detail is fine. The team plane reads through a
different, level-filtered path — never through these helpers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ... import models
from . import book

# Warmth ordering for member sorts: healthiest relationship first. "new"
# (never touched) sits between warm and cooling — an untouched contact is not
# yet cold, but they are not a live thread either.
_WARMTH_RANK = {"active": 0, "warm": 1, "new": 2, "cooling": 3, "dormant": 4}

# Never-touched contacts count as maximally stale in the strength rollup.
_MAX_STALE_DAYS = 100


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize DB datetimes: SQLite hands back naive values even when we
    stored tz-aware ones, and mixing the two in comparisons raises."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _as_utc(dt)
    return dt.isoformat() if dt else None


def _viewer_overlay(db: Session, viewer_user_id: int,
                    company_id: int) -> Optional[models.CompanyOverlay]:
    return (db.query(models.CompanyOverlay)
              .filter(models.CompanyOverlay.user_id == viewer_user_id,
                      models.CompanyOverlay.company_id == company_id)
              .first())


def _resolve_company(db: Session, company_id: int) -> Optional[models.Company]:
    """Fetch a Company, following merged-duplicate tombstones to the survivor
    (bounded so a bad merge cycle can't loop forever)."""
    company = db.get(models.Company, company_id)
    hops = 0
    while company is not None and company.merged_into_id and hops < 5:
        nxt = db.get(models.Company, company.merged_into_id)
        if nxt is None:
            break
        company, hops = nxt, hops + 1
    return company


def _company_view(company: models.Company,
                  overlay: Optional[models.CompanyOverlay]) -> dict:
    """Global Company row with the viewer's overlay corrections merged on
    top — the global row stays pipeline-owned; the overlay is how this viewer
    disagrees without touching anyone else's graph."""
    name = company.canonical_name
    if overlay is not None:
        try:
            corrections = json.loads(overlay.corrections_json or "{}")
        except (TypeError, ValueError):
            corrections = {}
        name = (corrections.get("canonical_name") or "").strip() or name
    return {"id": company.id, "canonical_name": name,
            "primary_domain": company.primary_domain}


def _company_name_for_viewer(db: Session, viewer_user_id: int,
                             company_id: int) -> Optional[str]:
    company = _resolve_company(db, company_id)
    if company is None:
        return None
    overlay = _viewer_overlay(db, viewer_user_id, company.id)
    return _company_view(company, overlay)["canonical_name"]


def _current_memberships(db: Session, account: models.Account,
                         viewer_user_id: int) -> list[models.AccountMembership]:
    """The viewer's own live person<->company edges for this account.
    Rejected memberships are the resolver being told "no" — never shown."""
    return (db.query(models.AccountMembership)
              .filter(models.AccountMembership.user_id == viewer_user_id,
                      models.AccountMembership.company_id == account.company_id,
                      models.AccountMembership.is_current.is_(True),
                      models.AccountMembership.status != "rejected")
              .all())


def _last_touch_by_contact(db: Session, viewer_user_id: int,
                           contact_ids: list[int]) -> dict[int, datetime]:
    """Latest interaction occurred_at per contact, one query."""
    if not contact_ids:
        return {}
    rows = (db.query(models.RelationshipInteraction.contact_id,
                     models.RelationshipInteraction.occurred_at)
              .filter(models.RelationshipInteraction.actor_user_id == viewer_user_id,
                      models.RelationshipInteraction.contact_id.in_(contact_ids))
              .all())
    out: dict[int, datetime] = {}
    for cid, occurred in rows:
        occurred = _as_utc(occurred)
        if occurred and (cid not in out or occurred > out[cid]):
            out[cid] = occurred
    return out


def _days_since(last_touch: Optional[datetime],
                fallback: Optional[datetime]) -> int:
    """Whole days since the last touch; a never-touched contact ages from its
    created_at so it reads stale, not fresh."""
    now = datetime.now(timezone.utc)
    anchor = _as_utc(last_touch) or _as_utc(fallback)
    if anchor is None:
        return _MAX_STALE_DAYS
    return max(0, (now - anchor).days)


def _member_contact_dict(contact: models.Contact, company_name: Optional[str],
                         days_since: int) -> dict:
    """The book-shaped contact dict score_health() expects — same keys
    routes/book.py's _book_from_spine_contacts builds (tier "core",
    cadence_days 30, review_due False are the spine defaults there too)."""
    return {
        "id": str(contact.id),
        "name": contact.name or "Unknown",
        "title": contact.title or contact.headline or "",
        "firm": company_name or contact.company or "",
        "tier": "core",
        "days_since": days_since,
        "cadence_days": 30,
        "review_due": False,
        "interaction_history": "",
    }


def _member_rows(db: Session, account: models.Account,
                 viewer_user_id: int, company_name: Optional[str]) -> list[dict]:
    """Current members joined to Contact, each with its score_health()
    verdict, sorted warmest first (health rank, then most-recent touch)."""
    memberships = _current_memberships(db, account, viewer_user_id)
    contact_ids = [m.contact_id for m in memberships]
    touch = _last_touch_by_contact(db, viewer_user_id, contact_ids)
    rows: list[dict] = []
    seen: set[int] = set()
    for m in memberships:
        if m.contact_id in seen:            # dupe edges (re-hire) collapse
            continue
        contact = db.get(models.Contact, m.contact_id)
        if contact is None:
            continue
        seen.add(m.contact_id)
        last = touch.get(m.contact_id)
        days = _days_since(last, contact.created_at)
        cdict = _member_contact_dict(contact, company_name, days)
        health = book.score_health(cdict) if last is not None else {
            # Never touched: "new", not "dormant" — there is no relationship
            # to have gone cold yet.
            "status": "new", "needs_outreach": False,
            "reason": "No interactions yet", "priority": 10,
        }
        rows.append({
            "contact_id": contact.id,
            "name": contact.name or "Unknown",
            "title": contact.title or contact.headline,
            "role_title": m.role_title,
            "days_since": days,
            "last_touch_at": _iso(last),
            "health": health,
        })
    rows.sort(key=lambda r: (_WARMTH_RANK.get(r["health"]["status"], 5),
                             r["days_since"], r["contact_id"]))
    return rows


# ── public read model ────────────────────────────────────────────────────────

def account_summary(db: Session, account: models.Account,
                    viewer_user_id: int) -> Optional[dict]:
    """List-row view of one account for its owner. Returns None when the
    viewer's overlay REJECTED this company grouping (their contacts should not
    be portrayed under it) — callers drop None rows."""
    company = _resolve_company(db, account.company_id)
    if company is None:
        return None
    overlay = _viewer_overlay(db, viewer_user_id, company.id)
    # The overlay may live on the pre-merge company id too.
    if overlay is None and company.id != account.company_id:
        overlay = _viewer_overlay(db, viewer_user_id, account.company_id)
    if overlay is not None and overlay.rejected:
        return None

    # Member preview: top 3 contact names by warmth. Warmth proxy here is
    # touch recency (most recently touched first) — deterministic and cheap
    # for the list endpoint; the detail view runs full score_health().
    memberships = _current_memberships(db, account, viewer_user_id)
    contact_ids = [m.contact_id for m in memberships]
    touch = _last_touch_by_contact(db, viewer_user_id, contact_ids)
    contacts = ([] if not contact_ids else
                db.query(models.Contact)
                  .filter(models.Contact.id.in_(contact_ids)).all())
    contacts.sort(key=lambda c: (_days_since(touch.get(c.id), c.created_at), c.id))
    preview = [c.name or "Unknown" for c in contacts[:3]]

    return {
        "id": account.id,
        "company": _company_view(company, overlay),
        "tier": account.tier,
        "starred": bool(account.starred),
        "objective": account.objective,
        "notes": account.notes,
        "sharing_level": account.sharing_level,
        "rollups": {
            "strength_score": account.strength_score,
            "last_touch_at": _iso(account.last_touch_at),
            "contact_count": account.contact_count,
            "warmest_contact_id": account.warmest_contact_id,
        },
        "member_preview": preview,
    }


def account_detail(db: Session, account: models.Account,
                   viewer_user_id: int) -> Optional[dict]:
    """Full account page: summary + members (warmth-sorted, with health) +
    former members ("now at X") + merged interaction timeline + coverage.
    None when the viewer rejected the grouping (mirrors account_summary)."""
    out = account_summary(db, account, viewer_user_id)
    if out is None:
        return None
    company_name = out["company"]["canonical_name"]

    members = _member_rows(db, account, viewer_user_id, company_name)
    out["members"] = members

    # Former members: closed edges at this company; if the contact has a
    # newer CURRENT edge elsewhere, surface where they went — each one is a
    # live thread into another account.
    former_edges = (db.query(models.AccountMembership)
                      .filter(models.AccountMembership.user_id == viewer_user_id,
                              models.AccountMembership.company_id == account.company_id,
                              models.AccountMembership.is_current.is_(False),
                              models.AccountMembership.status != "rejected")
                      .all())
    former: list[dict] = []
    current_ids = {m["contact_id"] for m in members}
    seen_former: set[int] = set()
    for edge in former_edges:
        # A contact with a live edge here too (re-hired) is a member, not a
        # former member.
        if edge.contact_id in current_ids or edge.contact_id in seen_former:
            continue
        seen_former.add(edge.contact_id)
        contact = db.get(models.Contact, edge.contact_id)
        if contact is None:
            continue
        newer = (db.query(models.AccountMembership)
                   .filter(models.AccountMembership.user_id == viewer_user_id,
                           models.AccountMembership.contact_id == edge.contact_id,
                           models.AccountMembership.is_current.is_(True),
                           models.AccountMembership.status != "rejected",
                           models.AccountMembership.company_id != account.company_id)
                   .first())
        former.append({
            "contact_id": contact.id,
            "name": contact.name or "Unknown",
            "ended_at": _iso(edge.ended_at),
            "now_at": (_company_name_for_viewer(db, viewer_user_id,
                                                newer.company_id)
                       if newer is not None else None),
        })
    out["former_members"] = former

    # Timeline: every current member-contact's interactions merged
    # chronologically, newest first, capped at 50.
    member_ids = [m["contact_id"] for m in members]
    name_by_id = {m["contact_id"]: m["name"] for m in members}
    interactions = ([] if not member_ids else
                    db.query(models.RelationshipInteraction)
                      .filter(models.RelationshipInteraction.actor_user_id == viewer_user_id,
                              models.RelationshipInteraction.contact_id.in_(member_ids))
                      .order_by(models.RelationshipInteraction.occurred_at.desc())
                      .limit(50)
                      .all())
    out["timeline"] = [{
        "contact_id": it.contact_id,
        "contact_name": name_by_id.get(it.contact_id, "Unknown"),
        "source_type": it.source_type,
        "interaction_type": it.interaction_type,
        "direction": it.direction,
        "occurred_at": _iso(it.occurred_at),
        "title": it.title,
        "summary": it.summary,
    } for it in interactions]

    # Coverage: warmth spread + the single-threaded warning (the account
    # hanging off <=1 warm contact is the classic key-account risk).
    statuses = [m["health"]["status"] for m in members]
    warm = sum(1 for s in statuses if s in ("active", "warm"))
    out["coverage"] = {
        "total": len(members),
        "warm": warm,
        "cooling": sum(1 for s in statuses if s == "cooling"),
        "dormant": sum(1 for s in statuses if s == "dormant"),
        "single_threaded": warm <= 1,
    }
    return out


def recompute_rollups(db: Session, account: models.Account) -> None:
    """Refresh the CACHED rollup fields on one account (never authoritative —
    always recomputable from memberships + interactions). Deterministic:

      strength_score     mean over current members of
                         (100 - min(days_since_last_touch, 100)); a member
                         never touched contributes 0. So 100 = every member
                         touched today, 0 = everyone stale/never touched.
      last_touch_at      max interaction occurred_at across current members.
      contact_count      number of distinct current member contacts.
      warmest_contact_id the most recently touched member contact.

    Does NOT commit — callers own the transaction.
    """
    memberships = _current_memberships(db, account, account.owner_id)
    contact_ids = sorted({m.contact_id for m in memberships})
    touch = _last_touch_by_contact(db, account.owner_id, contact_ids)

    account.contact_count = len(contact_ids)
    if not contact_ids:
        account.strength_score = None
        account.last_touch_at = None
        account.warmest_contact_id = None
        return

    freshness: list[float] = []
    warmest_id: Optional[int] = None
    warmest_days: Optional[int] = None
    for cid in contact_ids:
        last = touch.get(cid)
        days = (_MAX_STALE_DAYS if last is None
                else min(_days_since(last, None), _MAX_STALE_DAYS))
        freshness.append(float(_MAX_STALE_DAYS - days))
        if last is not None and (warmest_days is None or days < warmest_days):
            warmest_days, warmest_id = days, cid

    account.strength_score = round(sum(freshness) / len(freshness), 2)
    touches = [t for t in touch.values() if t is not None]
    account.last_touch_at = max(touches) if touches else None
    account.warmest_contact_id = warmest_id


def account_summaries_page(db: Session, accounts: list,
                           viewer_user_id: int) -> list[dict]:
    """Batched list-page assembly: the same rows account_summary() produces,
    built from ~6 queries TOTAL instead of ~6 per account.

    Why this exists: the app replicas and Postgres can sit in different
    regions, so every query pays a full cross-region round-trip (~100-200ms)
    even though execution is sub-millisecond. Per-account assembly turned a
    60-row page into 360 round-trips = ~47s on prod. Batch the page: one
    IN-query each for companies, overlays, memberships, touches, contacts,
    then assemble in memory. Latency now scales with query COUNT, not page
    size."""
    if not accounts:
        return []
    company_ids = {a.company_id for a in accounts}

    companies = {c.id: c for c in
                 db.query(models.Company)
                   .filter(models.Company.id.in_(company_ids)).all()}
    # Follow merge tombstones (bounded), batching each hop level.
    for _ in range(5):
        missing = {c.merged_into_id for c in companies.values()
                   if c.merged_into_id and c.merged_into_id not in companies}
        if not missing:
            break
        for c in (db.query(models.Company)
                    .filter(models.Company.id.in_(missing)).all()):
            companies[c.id] = c

    def survivor(cid):
        c, hops = companies.get(cid), 0
        while c is not None and c.merged_into_id and hops < 5:
            nxt = companies.get(c.merged_into_id)
            if nxt is None:
                break
            c, hops = nxt, hops + 1
        return c

    all_cids = set(companies)
    overlays = {}
    for o in (db.query(models.CompanyOverlay)
                .filter(models.CompanyOverlay.user_id == viewer_user_id,
                        models.CompanyOverlay.company_id.in_(all_cids)).all()):
        overlays[o.company_id] = o

    memberships: dict[int, list] = {}
    for m in (db.query(models.AccountMembership)
                .filter(models.AccountMembership.user_id == viewer_user_id,
                        models.AccountMembership.company_id.in_(company_ids),
                        models.AccountMembership.is_current.is_(True),
                        models.AccountMembership.status != "rejected").all()):
        memberships.setdefault(m.company_id, []).append(m)

    contact_ids = {m.contact_id for ms in memberships.values() for m in ms}
    touch = _last_touch_by_contact(db, viewer_user_id, list(contact_ids))
    contact_by_id = {} if not contact_ids else {
        c.id: c for c in db.query(models.Contact)
                           .filter(models.Contact.id.in_(contact_ids)).all()}

    out = []
    for account in accounts:
        company = survivor(account.company_id)
        if company is None:
            continue
        overlay = overlays.get(company.id) or overlays.get(account.company_id)
        if overlay is not None and overlay.rejected:
            continue
        members = [contact_by_id[m.contact_id]
                   for m in memberships.get(account.company_id, [])
                   if m.contact_id in contact_by_id]
        members.sort(key=lambda c: (_days_since(touch.get(c.id), c.created_at),
                                    c.id))
        out.append({
            "id": account.id,
            "company": _company_view(company, overlay),
            "tier": account.tier,
            "starred": bool(account.starred),
            "objective": account.objective,
            "notes": account.notes,
            "sharing_level": account.sharing_level,
            "rollups": {
                "strength_score": account.strength_score,
                "last_touch_at": _iso(account.last_touch_at),
                "contact_count": account.contact_count,
                "warmest_contact_id": account.warmest_contact_id,
            },
            "member_preview": [c.name or "Unknown" for c in members[:3]],
        })
    return out
