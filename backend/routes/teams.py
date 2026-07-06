"""
routes/teams.py : the team plane API (org layer).

Thin HTTP shell over agents/relationship/team_view.py — every relationship
read goes through that module's gates, and NO handler here touches
Contact/RelationshipInteraction/ContactFact directly, so this file cannot
grow a leak the gate layer doesn't see.

Access model (two deliberately different failure modes):
  * Non-members get 404 on every team-scoped route — the same
    existence-hiding discipline as get_owned_event, so outsiders can't
    probe which team ids exist or who runs them.
  * Members without the admin role get 403 on admin verbs (walls, invites,
    team settings) — they legitimately know the team exists, so hiding it
    would only confuse; what they lack is authority.

Invite tokens are stateless HMAC capabilities (team id + expiry + signature
over the app's invite secret) rather than a DB table: same "unguessable
random" posture as auth's session tokens, minus a table we'd have to sweep.
SURPLUS_INVITE_SECRET pins them across restarts/replicas; without it a
per-process secret still yields unguessable, expiring tokens (outstanding
invites die on restart — acceptable for the current single-process deploy).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..agents.relationship import team_view
from ..auth import current_user
from ..db import get_db

router = APIRouter(prefix="/api/teams", tags=["teams"])

COMPLIANCE_PROFILES = ("collaborative", "strict")
VIEW_STATES = ("live", "pending")


# ─── invite tokens ──────────────────────────────────────────────────────────

INVITE_TTL_SECONDS = 7 * 24 * 3600

# Per-process fallback secret. Random (never a hardcoded default), so tokens
# stay unguessable even when SURPLUS_INVITE_SECRET is unset.
_FALLBACK_INVITE_SECRET = secrets.token_urlsafe(32)


def _invite_secret() -> bytes:
    return (os.environ.get("SURPLUS_INVITE_SECRET")
            or _FALLBACK_INVITE_SECRET).encode()


def _invite_sig(team_id: int, expires: int) -> str:
    msg = f"team-invite:{team_id}:{expires}".encode()
    return hmac.new(_invite_secret(), msg, hashlib.sha256).hexdigest()[:32]


def mint_invite_token(team_id: int) -> str:
    """Short signed capability: "<team_id>.<expiry_ts>.<sig>". Carries no
    user identity — whoever presents it joins as themselves — which is
    exactly the semantics of handing a teammate an invite link."""
    expires = int(time.time()) + INVITE_TTL_SECONDS
    return f"{team_id}.{expires}.{_invite_sig(team_id, expires)}"


def _verify_invite_token(token: str, team_id: int) -> bool:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return False
    try:
        tid, expires = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if tid != team_id or expires < time.time():
        return False
    return hmac.compare_digest(parts[2], _invite_sig(tid, expires))


# ─── access helpers ─────────────────────────────────────────────────────────

def _team_or_404(db: Session, team_id: int) -> models.Team:
    team = db.get(models.Team, team_id)
    if team is None:
        raise HTTPException(404, "team not found")
    return team


def _member_or_404(db: Session, team_id: int,
                   user: models.User) -> Tuple[models.Team, models.TeamMembership]:
    """Membership IS the read credential for the team plane. 404 (not 403)
    for non-members so the response is indistinguishable from a nonexistent
    team — outsiders learn nothing, not even that there is something to be
    denied access to."""
    team = _team_or_404(db, team_id)
    m = (db.query(models.TeamMembership)
         .filter(models.TeamMembership.team_id == team.id,
                 models.TeamMembership.user_id == user.id)
         .first())
    if m is None:
        raise HTTPException(404, "team not found")
    return team, m


def _admin_or_403(membership: models.TeamMembership) -> None:
    """Admin verbs (walls, invites, policy) 403 for plain members: they know
    the team exists, so the honest answer is "not yours to do"."""
    if membership.role != "admin":
        raise HTTPException(403, "admin role required")


def _membership_brief(team: models.Team, m: models.TeamMembership) -> dict:
    return {
        "team_id": team.id,
        "name": team.name,
        "role": m.role,
        "compliance_profile": team.compliance_profile,
        "view_state": team.view_state,
        "share_signals": m.share_signals,
    }


# ─── request bodies ─────────────────────────────────────────────────────────

class TeamCreate(BaseModel):
    name: str
    compliance_profile: Optional[str] = None


class TeamPatch(BaseModel):
    compliance_profile: Optional[str] = None
    view_state: Optional[str] = None


class JoinBody(BaseModel):
    invite_token: str


class MemberPatch(BaseModel):
    share_signals: bool


class WallCreate(BaseModel):
    company_id: Optional[int] = None
    name_norm: Optional[str] = None
    excluded_user_ids: Optional[List[int]] = None
    reason: Optional[str] = None


# ─── team lifecycle ─────────────────────────────────────────────────────────

@router.post("")
def create_team(body: TeamCreate, db: Session = Depends(get_db),
                user: models.User = Depends(current_user)):
    """Create a team; the creator becomes its admin. A strict profile starts
    with view_state="pending" (the conflict-import interlock): walls must
    exist before anything is visible, so the view stays dark until the admin
    imports conflicts or explicitly flips it live."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "team name required")
    profile = (body.compliance_profile or "collaborative").strip().lower()
    if profile not in COMPLIANCE_PROFILES:
        raise HTTPException(400, f"compliance_profile must be one of {COMPLIANCE_PROFILES}")
    team = models.Team(
        name=name[:120],
        compliance_profile=profile,
        view_state="pending" if profile == "strict" else "live",
        created_by=user.id,
    )
    db.add(team)
    db.flush()
    m = models.TeamMembership(team_id=team.id, user_id=user.id, role="admin")
    db.add(m)
    db.commit()
    return _membership_brief(team, m)


@router.get("/mine")
def my_teams(db: Session = Depends(get_db),
             user: models.User = Depends(current_user)):
    """Teams the caller belongs to, with role + policy state (the SPA needs
    view_state to render the pending interlock screen)."""
    rows = (db.query(models.TeamMembership, models.Team)
            .join(models.Team,
                  models.Team.id == models.TeamMembership.team_id)
            .filter(models.TeamMembership.user_id == user.id)
            .all())
    return {"teams": [_membership_brief(team, m) for m, team in rows]}


@router.patch("/{team_id}")
def patch_team(team_id: int, body: TeamPatch, db: Session = Depends(get_db),
               user: models.User = Depends(current_user)):
    """Admin policy switchboard. Flipping view_state to "live" is the
    conflict-import-done unlock for strict teams; both flips are audited
    (log line) because policy changes are compliance evidence."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    if body.compliance_profile is not None:
        profile = body.compliance_profile.strip().lower()
        if profile not in COMPLIANCE_PROFILES:
            raise HTTPException(400, f"compliance_profile must be one of {COMPLIANCE_PROFILES}")
        team.compliance_profile = profile
    if body.view_state is not None:
        state = body.view_state.strip().lower()
        if state not in VIEW_STATES:
            raise HTTPException(400, f"view_state must be one of {VIEW_STATES}")
        team.view_state = state
    db.commit()
    print(f"  [teams.audit] team {team.id} policy set by user {user.id}: "
          f"profile={team.compliance_profile} view_state={team.view_state}")
    return _membership_brief(team, m)


# ─── membership ─────────────────────────────────────────────────────────────

@router.post("/{team_id}/invite")
def create_invite(team_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)):
    """Admin mints a signed, expiring join token. Stateless on purpose: the
    capability is the signature, and joining still requires a signed-in
    surplus session (the token names the team, never the person)."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    return {"invite_token": mint_invite_token(team.id),
            "expires_in": INVITE_TTL_SECONDS}


@router.post("/{team_id}/join")
def join_team(team_id: int, body: JoinBody, db: Session = Depends(get_db),
              user: models.User = Depends(current_user)):
    """Join with a valid invite token. This is consent-at-the-edge: the
    membership row this creates is what turns on Level-1 sharing of the
    joiner's edges (share_signals defaults True; the kill switch below
    revokes it any time). Nothing is copied on join — the team view is a
    query-time join, so there is nothing to import here."""
    team = _team_or_404(db, team_id)
    if not _verify_invite_token(body.invite_token, team.id):
        raise HTTPException(403, "invalid invite token")
    existing = (db.query(models.TeamMembership)
                .filter(models.TeamMembership.team_id == team.id,
                        models.TeamMembership.user_id == user.id)
                .first())
    if existing is not None:
        return _membership_brief(team, existing)
    m = models.TeamMembership(team_id=team.id, user_id=user.id, role="member")
    db.add(m)
    db.commit()
    return _membership_brief(team, m)


@router.delete("/{team_id}/members/me")
def leave_team(team_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(current_user)):
    """Leave the team. Because the team plane is assembled at query time,
    deleting the membership row removes this member's edges from every
    teammate's aggregates on the very next request — nothing was ever
    copied, so there is nothing to claw back (the departure guarantee in
    the design doc §6, and the ownership pitch to individual users)."""
    team, m = _member_or_404(db, team_id, user)
    db.delete(m)
    db.commit()
    print(f"  [teams.audit] user {user.id} left team {team.id}")
    return {"ok": True}


@router.patch("/{team_id}/members/me")
def patch_my_membership(team_id: int, body: MemberPatch,
                        db: Session = Depends(get_db),
                        user: models.User = Depends(current_user)):
    """The per-user kill switch. share_signals=False pulls all of this
    member's edges out of the pool (consent is revocable) while their
    viewing rights remain — viewing is membership, sharing is consent."""
    team, m = _member_or_404(db, team_id, user)
    m.share_signals = bool(body.share_signals)
    db.commit()
    print(f"  [teams.audit] user {user.id} set share_signals="
          f"{m.share_signals} on team {team.id}")
    return _membership_brief(team, m)


@router.get("/{team_id}/members")
def list_members(team_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(current_user)):
    """The team roster: any member may see who is on their own team (name,
    role, sharing state). Needed by the wall admin UI's exclusion picker —
    ids alone can't render a screen list. Roster only: nothing about anyone's
    relationships travels through here."""
    team, _ = _member_or_404(db, team_id, user)
    rows = (db.query(models.TeamMembership, models.User)
              .join(models.User,
                    models.User.id == models.TeamMembership.user_id)
              .filter(models.TeamMembership.team_id == team.id)
              .order_by(models.TeamMembership.joined_at)
              .all())
    return {"members": [{
        "user_id": u.id,
        "name": (u.name or u.email or f"user {u.id}"),
        "role": m.role,
        "share_signals": bool(m.share_signals),
    } for m, u in rows]}


# ─── relationship reads (all through team_view's gates) ─────────────────────

@router.get("/{team_id}/accounts")
def team_accounts(team_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)):
    """The team account list: every company where any consenting member has
    a current linked path, as THIS viewer is allowed to see it (walls remove
    companies from lists and counts per-viewer)."""
    team, _ = _member_or_404(db, team_id, user)
    return team_view.team_accounts(db, team, user.id)


@router.get("/{team_id}/companies/{company_id}/paths")
def company_paths(team_id: int, company_id: int,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(current_user)):
    """Who knows whom at this company — Level-1 rows only. 404s identically
    for an unknown company and a company walled for this viewer: a
    distinguishable error would reveal that the wall (and the subject)
    exists."""
    team, _ = _member_or_404(db, team_id, user)
    res = team_view.company_paths(db, team, user.id, company_id)
    if res is None:
        raise HTTPException(404, "company not found")
    return res


@router.get("/{team_id}/search")
def search(team_id: int, q: str = "", db: Session = Depends(get_db),
           user: models.User = Depends(current_user)):
    """Company-name search across the team view. Runs on the gated rollups,
    so walled subjects are unfindable for excluded viewers — not just
    unlisted."""
    team, _ = _member_or_404(db, team_id, user)
    return team_view.search_companies(db, team, user.id, q)


# ─── ethical walls (admin only) ─────────────────────────────────────────────

def _wall_brief(w: models.Wall) -> dict:
    try:
        excluded = json.loads(w.excluded_user_ids or "[]")
    except (ValueError, TypeError):
        excluded = []
    return {
        "wall_id": w.id,
        "subject_company_id": w.subject_company_id,
        "subject_name_norm": w.subject_name_norm,
        "excluded_user_ids": excluded,
        "reason": w.reason,
        "created_by": w.created_by,
    }


@router.get("/{team_id}/walls")
def list_walls(team_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(current_user)):
    """Walls are admin-only reads too: the exclusion list itself reveals
    which members are conflicted on which subjects."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    rows = db.query(models.Wall).filter(models.Wall.team_id == team.id).all()
    # Enrich id-based walls with the company's display name (one IN-query);
    # name_norm walls carry their own string.
    cids = {w.subject_company_id for w in rows if w.subject_company_id}
    names = {} if not cids else {
        c.id: c.canonical_name
        for c in db.query(models.Company)
                   .filter(models.Company.id.in_(cids)).all()}
    out = []
    for w in rows:
        brief = _wall_brief(w)
        brief["company_name"] = names.get(w.subject_company_id)
        out.append(brief)
    return {"walls": out}


@router.post("/{team_id}/walls")
def create_wall(team_id: int, body: WallCreate, db: Session = Depends(get_db),
                user: models.User = Depends(current_user)):
    """Create an ethical wall on a company (by id) or on a normalized name
    (the provisional conflict-import fail-safe — walls the string before
    entity resolution completes). Omitted/empty excluded_user_ids means the
    wall applies to ALL members. Takes effect on the next query — there is
    no copied state to scrub."""
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    norm = (body.name_norm or "").strip().lower() or None
    if body.company_id is None and norm is None:
        raise HTTPException(400, "one of company_id or name_norm is required")
    if body.company_id is not None and db.get(models.Company, body.company_id) is None:
        raise HTTPException(404, "company not found")
    w = models.Wall(
        team_id=team.id,
        subject_kind="company",
        subject_company_id=body.company_id,
        subject_name_norm=norm,
        excluded_user_ids=json.dumps(body.excluded_user_ids or []),
        reason=(body.reason or "")[:300] or None,
        created_by=user.id,
    )
    db.add(w)
    db.commit()
    # Wall create/modify/delete are audited events — the audit trail is the
    # compliance evidence a firm shows to demonstrate the screen.
    print(f"  [teams.audit] wall {w.id} created on team {team.id} by user "
          f"{user.id}: company={w.subject_company_id} "
          f"name_norm={w.subject_name_norm!r} excluded={w.excluded_user_ids}")
    return _wall_brief(w)


@router.delete("/{team_id}/walls/{wall_id}")
def delete_wall(team_id: int, wall_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(current_user)):
    team, m = _member_or_404(db, team_id, user)
    _admin_or_403(m)
    w = (db.query(models.Wall)
         .filter(models.Wall.id == wall_id, models.Wall.team_id == team.id)
         .first())
    if w is None:
        raise HTTPException(404, "wall not found")
    db.delete(w)
    db.commit()
    print(f"  [teams.audit] wall {wall_id} deleted on team {team.id} "
          f"by user {user.id}")
    return {"ok": True}
