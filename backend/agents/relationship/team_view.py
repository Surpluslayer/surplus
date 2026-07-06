"""
team_view.py : gated Level-1 aggregate assembly for the team plane.

The team plane is a LENS over members' per-user graphs (docs/
accounts-architecture.md §6): nothing is copied on join, so every read here
re-derives the view from source rows and re-applies every confidentiality
gate at query time. That is exactly what makes walls take effect on the very
next query and departures instant — there is never a materialized copy to
scrub, and a member who leaves simply stops matching the membership join.

Gate order (fixed, enforced BEFORE any aggregation):

  0. Strict interlock — a "strict"-profile team whose view_state is still
     "pending" (conflict import not finished/skipped) blanks EVERY
     relationship read. Checked first so a pending team can't even leak
     whether a given company exists in the pool.
  1. Ethical wall — for an excluded viewer the walled company CEASES TO
     EXIST: absent from lists, counts, and search, because revealing that a
     relationship exists can itself be a breach. Bidirectional: an excluded
     member's own edges into the subject are withheld from every OTHER
     member's aggregates, so nobody can route a path through someone behind
     the wall. `excluded_user_ids == "[]"` means the wall applies to ALL
     members (the imported-conflict fail-safe). A wall beats every sharing
     level below.
  2. Kill switch — TeamMembership.share_signals=False pulls all of that
     member's edges out of the pool (consent is revocable at any time).
     They can still VIEW the team plane: viewing is membership; sharing is
     consent, and the two are deliberately independent.
  3. Owner level — Account.sharing_level "private" (Level 0) hides that
     owner's edges to that company from the whole team plane, including the
     owner's own team view (their personal book is untouched). "metadata"
     and "elevated" both render the Level-1 shape for now (Level 2 arrives
     with the collaborative timeline surface).
  4. Edge validity — only AccountMembership rows with status="linked" and
     is_current=True contribute. Pending-review, rejected, and historical
     edges are not team signal.

Level 1 is the ONLY shape this module emits about a relationship:
{member_name, contact_name, contact_title, warmth_band, last_touch_band}.
The serializer builds that dict field-by-field from scalars — never
model_dump()/__dict__ of an ORM row — so a new column on any source table
can never ride along by accident. No message bodies, facts, notes,
interaction titles/summaries, raw timestamps, emails, or phones exist
anywhere downstream of this module.

Every read over these view models is audited at the route layer
(routes/teams.py -> agents/relationship/audit.write, best-effort so audit
trouble never takes down a view): who viewed which aggregate, when, with
RESULT COUNTS only. That contract leans on the Level-1 discipline above —
because nothing below this docstring emits relationship content, the counts
the routes log (accounts returned, path rows, search hits) are Class-B
metadata and safe to persist in TeamAuditLog.detail_json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models
from .book import _score_health_heuristic

# The Level-1 contract. Tests pin response rows to exactly this key set.
LEVEL1_FIELDS = ("member_name", "contact_name", "contact_title",
                 "warmth_band", "last_touch_band")

# Warmth vocabulary is score_health's status vocabulary, best-first. The
# rank is used to pick an account's "best band" without ever exposing the
# underlying scores or dates.
WARMTH_BANDS = ("active", "warm", "cooling", "dormant")
_WARMTH_RANK = {b: i for i, b in enumerate(WARMTH_BANDS)}

# Recency is deliberately coarse (a band, not a timestamp): "last touched
# Tuesday 9:41pm" is Class C texture; "this week" is Class B metadata.
LAST_TOUCH_BANDS = ("this week", "this month", "older", "never")

# The one payload every relationship endpoint returns while a strict team's
# conflict import is unfinished. No data keys at all, so a serializer bug
# can't leak "empty but present" structure that differs by pool contents.
PENDING = {"view_state": "pending"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes, Postgres strips tz on DateTime
    columns; coerce to aware UTC so date math never raises (same fix as
    auth._as_aware_utc)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def view_pending(team: "models.Team") -> bool:
    """Gate 0. Strict teams stay dark until the admin finishes (or
    audited-skips) the conflict import — walls must exist before anything is
    visible, so deferring the import never opens an exposure gap."""
    return (team.compliance_profile == "strict"
            and team.view_state == "pending")


# ── warmth / recency banding ────────────────────────────────────────────────

def warmth_band(last_touch: Optional[datetime]) -> str:
    """Band one relationship's warmth, reusing book.score_health's status
    vocabulary and math. We call the deterministic heuristic directly rather
    than score_health()'s LLM wrapper for two structural reasons: Level 1 is
    Class-B metadata only, and the LLM prompt reads interaction_history
    (Class C content) — feeding it here would leak content into a shared
    surface; and §9.6 forbids model latency on the read path (LLM work
    belongs to the sweep). The heuristic consumes nothing but day counts.

    A never-touched contact has no warmth to share: dormant."""
    if last_touch is None:
        return "dormant"
    days = max(0, int((_utcnow() - _aware(last_touch)).total_seconds() // 86400))
    out = _score_health_heuristic({"days_since": days, "cadence_days": 90})
    status = out.get("status")
    return status if status in WARMTH_BANDS else "dormant"


def last_touch_band(last_touch: Optional[datetime]) -> str:
    """Coarse recency band. Raw timestamps are deliberately destroyed here:
    a teammate needs to know a path is fresh, not reconstruct the owner's
    meeting calendar."""
    if last_touch is None:
        return "never"
    days = (_utcnow() - _aware(last_touch)).days
    if days < 7:
        return "this week"
    if days < 31:
        return "this month"
    return "older"


def _best_band(bands: List[str]) -> str:
    """Warmest band wins ("does the team have a live path?"), never an
    average that would hint at how many cold edges sit behind it."""
    if not bands:
        return "dormant"
    return min(bands, key=lambda b: _WARMTH_RANK.get(b, len(WARMTH_BANDS)))


# ── gate 1: ethical walls ───────────────────────────────────────────────────

def _wall_index(db: Session, team_id: int) -> List[Tuple[Set[int], Optional[Set[int]]]]:
    """Resolve every wall on the team to (walled company ids, excluded user
    ids). A name_norm wall (the provisional conflict-import fail-safe) is
    resolved through CompanyIdentity at read time, so a Company that gains a
    matching name identity AFTER the wall was written is walled from its
    first appearance — over-walling by design, per the error asymmetry in
    §6b. excluded=None means the wall applies to ALL members ("[]" in the
    row)."""
    out: List[Tuple[Set[int], Optional[Set[int]]]] = []
    for w in db.query(models.Wall).filter(models.Wall.team_id == team_id):
        ids: Set[int] = set()
        if w.subject_company_id is not None:
            ids.add(w.subject_company_id)
        norm = (w.subject_name_norm or "").strip().lower()
        if norm:
            for (cid,) in (db.query(models.CompanyIdentity.company_id)
                           .filter(models.CompanyIdentity.kind == "name_norm",
                                   models.CompanyIdentity.value == norm)):
                ids.add(cid)
        if not ids:
            continue  # unresolvable wall rows wall nothing (yet)
        try:
            raw = json.loads(w.excluded_user_ids or "[]")
        except (ValueError, TypeError):
            # A corrupt exclusion list fails toward over-walling (everyone),
            # never toward exposure.
            raw = []
        excluded = {int(u) for u in raw} if raw else None
        out.append((ids, excluded))
    return out


def _walled_companies_for(user_id: int,
                          wall_index: List[Tuple[Set[int], Optional[Set[int]]]]) -> Set[int]:
    """Company ids that do not exist for `user_id` on this team plane."""
    walled: Set[int] = set()
    for company_ids, excluded in wall_index:
        if excluded is None or user_id in excluded:
            walled |= company_ids
    return walled


# ── the gated edge pool ─────────────────────────────────────────────────────

def _gated_edges(db: Session, team: "models.Team", viewer_id: int) -> List[dict]:
    """Assemble the pool of relationship edges `viewer_id` may aggregate
    over, with gates 1-4 applied row-by-row BEFORE anything is counted or
    serialized. Returns plain dicts of pre-vetted scalars (no ORM rows leave
    this function), each carrying only what the Level-1 serializer and the
    per-company rollups need."""
    memberships = (db.query(models.TeamMembership)
                   .filter(models.TeamMembership.team_id == team.id).all())
    # Gate 2: only consenting members contribute edges. The viewer's own
    # membership grants viewing; it does not exempt them from any gate.
    sharing_ids = {m.user_id for m in memberships if m.share_signals}
    if not sharing_ids:
        return []

    walls = _wall_index(db, team.id)
    # Gate 1 inbound: subjects that don't exist for the viewer.
    viewer_walled = _walled_companies_for(viewer_id, walls)
    # Gate 1 outbound: each contributor's own walled subjects, so an excluded
    # member's edges never surface in anyone else's aggregate.
    member_walled: Dict[int, Set[int]] = {
        uid: _walled_companies_for(uid, walls) for uid in sharing_ids
    }

    # Gate 3: owners who marked this company private (Level 0). Absence of an
    # Account row means the default level ("metadata") — it contributes.
    private_pairs = {
        (a.owner_id, a.company_id)
        for a in db.query(models.Account)
        .filter(models.Account.owner_type == "user",
                models.Account.owner_id.in_(sharing_ids),
                models.Account.sharing_level == "private")
    }

    rows = (db.query(models.AccountMembership, models.Contact,
                     models.Company, models.User)
            .join(models.Contact,
                  models.Contact.id == models.AccountMembership.contact_id)
            .join(models.Company,
                  models.Company.id == models.AccountMembership.company_id)
            .join(models.User,
                  models.User.id == models.AccountMembership.user_id)
            # Gate 4: current, resolver-confirmed edges only.
            .filter(models.AccountMembership.user_id.in_(sharing_ids),
                    models.AccountMembership.status == "linked",
                    models.AccountMembership.is_current.is_(True))
            .all())

    edges: List[dict] = []
    for am, contact, company, member in rows:
        cid = company.id
        if cid in viewer_walled:                      # gate 1 (inbound)
            continue
        if cid in member_walled.get(am.user_id, ()):  # gate 1 (outbound)
            continue
        if (am.user_id, cid) in private_pairs:        # gate 3
            continue
        edges.append({
            "member_user_id": am.user_id,
            "member_name": member.name or member.email or f"member {member.id}",
            "contact_id": contact.id,
            "contact_name": contact.name or "",
            # role_title is the membership's own claim; the contact's watched
            # title is the fallback. Titles are public-profile data (Class A/B).
            "contact_title": am.role_title or contact.title or None,
            "company_id": cid,
            "company_name": company.canonical_name,
        })
    return edges


def _last_touch_map(db: Session, contact_ids: Set[int]) -> Dict[int, datetime]:
    """Latest interaction timestamp per contact — consumed ONLY to compute
    bands. The raw datetimes never leave this module."""
    if not contact_ids:
        return {}
    rows = (db.query(models.RelationshipInteraction.contact_id,
                     func.max(models.RelationshipInteraction.occurred_at))
            .filter(models.RelationshipInteraction.contact_id.in_(contact_ids))
            .group_by(models.RelationshipInteraction.contact_id)
            .all())
    return {cid: ts for cid, ts in rows if cid is not None and ts is not None}


def _level1_row(edge: dict, last_touch: Optional[datetime]) -> dict:
    """THE Level-1 serializer — the only relationship shape the team plane
    emits. Built key-by-key from scalars precisely so that adding a field to
    Contact/AccountMembership/anything else can never widen this payload
    without an explicit edit here (and a matching test change)."""
    return {
        "member_name": edge["member_name"],
        "contact_name": edge["contact_name"],
        "contact_title": edge["contact_title"],
        "warmth_band": warmth_band(last_touch),
        "last_touch_band": last_touch_band(last_touch),
    }


# ── public read model ───────────────────────────────────────────────────────

def _account_rows(db: Session, team: "models.Team", viewer_id: int) -> List[dict]:
    """Per-company rollups over the gated pool. Counts are computed AFTER
    the gates, so "2 people know someone at Acme" can never include a walled
    or private or non-consenting edge — the count itself is Class B and must
    not leak what the gates removed."""
    edges = _gated_edges(db, team, viewer_id)
    touches = _last_touch_map(db, {e["contact_id"] for e in edges})
    by_company: Dict[int, dict] = {}
    for e in edges:
        row = by_company.setdefault(e["company_id"], {
            "company_id": e["company_id"],
            "company_name": e["company_name"],
            "_members": set(),
            "_bands": [],
        })
        row["_members"].add(e["member_user_id"])
        row["_bands"].append(warmth_band(touches.get(e["contact_id"])))
    out = []
    for row in by_company.values():
        out.append({
            "company_id": row["company_id"],
            "company_name": row["company_name"],
            "member_count": len(row["_members"]),
            "path_count": len(row["_bands"]),
            "warmth": _best_band(row["_bands"]),
        })
    out.sort(key=lambda r: (_WARMTH_RANK.get(r["warmth"], 9),
                            r["company_name"].lower()))
    return out


def team_accounts(db: Session, team: "models.Team", viewer_id: int) -> dict:
    """GET /accounts read model: every company where any consenting member
    has a current linked path, as the viewer is allowed to see it."""
    if view_pending(team):
        return dict(PENDING)
    return {"view_state": "live",
            "accounts": _account_rows(db, team, viewer_id)}


def company_paths(db: Session, team: "models.Team", viewer_id: int,
                  company_id: int) -> Optional[dict]:
    """The "who knows whom at Acme" answer: Level-1 rows only. Returns None
    when the company must not exist for this viewer (unknown id OR walled),
    so the route can 404 identically in both cases — distinguishing them
    would itself reveal the wall."""
    if view_pending(team):
        # Checked before the company lookup: a pending team must not even
        # confirm which company ids resolve.
        return dict(PENDING)
    company = db.get(models.Company, company_id)
    if company is None:
        return None
    walls = _wall_index(db, team.id)
    if company_id in _walled_companies_for(viewer_id, walls):
        return None
    edges = [e for e in _gated_edges(db, team, viewer_id)
             if e["company_id"] == company_id]
    touches = _last_touch_map(db, {e["contact_id"] for e in edges})
    return {
        "view_state": "live",
        "company_name": company.canonical_name,
        "paths": [_level1_row(e, touches.get(e["contact_id"])) for e in edges],
    }


def search_companies(db: Session, team: "models.Team", viewer_id: int,
                     q: str) -> dict:
    """Company-name search over the TEAM VIEW, not over the Company table:
    matching runs on the already-gated account rows, so a walled company is
    not merely unlisted — it is unfindable, with zero result-count side
    channel."""
    if view_pending(team):
        return dict(PENDING)
    needle = (q or "").strip().lower()
    if not needle:
        return {"view_state": "live", "results": []}
    hits = [r for r in _account_rows(db, team, viewer_id)
            if needle in r["company_name"].lower()]
    return {"view_state": "live", "results": hits}
