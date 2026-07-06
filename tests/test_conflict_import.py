"""
Tests for the conflict-import flow (routes/team_conflicts.py +
agents/relationship/conflict_import.py) — the deterministic gates of
docs/accounts-architecture.md §6b.

What is under test is the fail-safe direction, not endpoint plumbing:
  (a) every input line lands in exactly one state (coverage invariant) and
      each unique name gets a provisional wall,
  (b) a provisional name-wall ENFORCES immediately, before any review,
  (c) confirm narrows single-match walls to entity walls (keeping name_norm)
      and flips the strict interlock live; ambiguity stays over-walled,
  (d) skip requires a reason and is audited,
  (e) re-import is idempotent,
  (f) 403/404 discipline matches the teams router,
  (g) audit rows exist for import/confirm/skip.

Fixture style follows tests/test_teams_api.py: in-memory SQLite, real ORM
rows, real TestClient + Bearer auth. Both routers are mounted because the
enforcement assertions go through the teams read endpoints.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.agents.relationship import conflict_import as ci
from backend.db import Base, get_db
from backend.routes import team_conflicts as conflicts_route
from backend.routes import teams as teams_route


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SURPLUS_INVITE_SECRET", "test-invite-secret")
    yield


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    # main.py registers routers in prod; here both are mounted on a bare app
    # (teams for setup + enforcement reads, team_conflicts under test).
    app = FastAPI()
    app.include_router(teams_route.router)
    app.include_router(conflicts_route.router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


# ── row builders (as in test_teams_api) ─────────────────────────────────────

def _user(db, name, email):
    u = models.User(name=name, email=email)
    db.add(u)
    db.commit()
    return u


def _auth(db, user):
    tok = secrets.token_urlsafe(24)
    db.add(models.Session(session_token=tok, user_id=user.id,
                          expires_at=_now() + timedelta(days=1)))
    db.commit()
    return {"Authorization": f"Bearer {tok}"}


def _company(db, name, norm=None):
    c = models.Company(canonical_name=name)
    db.add(c)
    db.flush()
    if norm is not None:
        db.add(models.CompanyIdentity(company_id=c.id, kind="name_norm",
                                      value=norm))
    db.commit()
    return c


def _edge(db, user, company, contact_name, title, days_since=None):
    contact = models.Contact(
        user_id=user.id,
        primary_identity_key=f"li:{contact_name.lower().replace(' ', '-')}-{user.id}",
        name=contact_name, title=title,
    )
    db.add(contact)
    db.flush()
    db.add(models.AccountMembership(
        user_id=user.id, contact_id=contact.id, company_id=company.id,
        role_title=title, is_current=True, status="linked",
    ))
    if (db.query(models.Account)
          .filter(models.Account.owner_type == "user",
                  models.Account.owner_id == user.id,
                  models.Account.company_id == company.id).first()) is None:
        db.add(models.Account(owner_type="user", owner_id=user.id,
                              company_id=company.id))
    if days_since is not None:
        db.add(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id,
            source_type="manual_note", interaction_type="note",
            occurred_at=_now() - timedelta(days=days_since),
            title="note", summary="note"))
    db.commit()
    return contact


@pytest.fixture
def setup(db, client):
    """Admin A + member B on a live collaborative team, plus an outsider.
    Companies:
      Acme Capital — identity "acme capital" (single match for "Acme Capital")
      Meridian, Inc. / Meridian LLC — both canonical names normalize to
        "meridian", so an imported "Meridian" is ambiguous (multi-match);
        only mer1 has an identity row, so mer2 must arrive via the live
        canonical-name scan (both matching paths exercised)
      Globex — identity "globex"
    B has an edge into Acme so the enforcement test has something to hide."""
    a = _user(db, "Daniel", "daniel@example.com")
    b = _user(db, "Cofounder", "cof@example.com")
    o = _user(db, "Outsider", "out@example.com")
    ha, hb, ho = _auth(db, a), _auth(db, b), _auth(db, o)

    acme = _company(db, "Acme Capital", "acme capital")
    globex = _company(db, "Globex", "globex")
    # Two live companies whose canonical names BOTH normalize to "meridian"
    # (legal suffixes fold away). Identity only on one — the multi-match must
    # come from the live-Company scan, exercising both matching paths.
    mer1 = _company(db, "Meridian, Inc.", "meridian")
    mer2 = _company(db, "Meridian LLC")

    _edge(db, a, acme, "Jane Doe", "Partner", days_since=3)
    _edge(db, b, acme, "Kate Liu", "Principal", days_since=10)
    _edge(db, b, globex, "Nate Fox", "COO", days_since=20)

    r = client.post("/api/teams", json={"name": "Surplus"}, headers=ha)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    team_id = r.json()["team_id"]
    tok = client.post(f"/api/teams/{team_id}/invite",
                      headers=ha).json()["invite_token"]
    assert client.post(f"/api/teams/{team_id}/join",
                       json={"invite_token": tok},
                       headers=hb).status_code == 200

    return SimpleNamespace(db=db, client=client, a=a, b=b, o=o,
                           ha=ha, hb=hb, ho=ho, team=team_id,
                           acme=acme, globex=globex, mer1=mer1, mer2=mer2)


def _strict_team(s):
    """A second, strict team (view pending) with A admin + B member."""
    r = s.client.post("/api/teams",
                      json={"name": "Firm", "compliance_profile": "strict"},
                      headers=s.ha)
    assert r.status_code == 200 and r.json()["view_state"] == "pending"
    tid = r.json()["team_id"]
    tok = s.client.post(f"/api/teams/{tid}/invite",
                        headers=s.ha).json()["invite_token"]
    s.client.post(f"/api/teams/{tid}/join", json={"invite_token": tok},
                  headers=s.hb)
    return tid


def _import(s, team, text, headers=None):
    return s.client.post(f"/api/teams/{team}/conflicts/import",
                         json={"text": text}, headers=headers or s.ha)


def _walls(db, team_id):
    return (db.query(models.Wall)
              .filter(models.Wall.team_id == team_id)
              .order_by(models.Wall.id).all())


def _audit_rows(db, team_id, event):
    return (db.query(models.TeamAuditLog)
              .filter(models.TeamAuditLog.team_id == team_id,
                      models.TeamAuditLog.event == event)
              .order_by(models.TeamAuditLog.id).all())


# ── (a) parse + provisional walls + coverage invariant ──────────────────────

MESSY_LIST = (
    "\ufeffCompany\n"                       # BOM + obvious header
    "Acme Capital, Inc.\n"                  # unquoted CSV -> first cell
    "\n"                                    # empty
    '"Globex, LLC",some,extra,cols\n'       # quoted first cell keeps comma
    "  Acme Capital  \n"                    # duplicate of line 2 (post-norm)
    "\x07Wei\x01rd Co\n"                    # control chars stripped, walled
    "***\n"                                 # punctuation-only -> empty norm
)


def test_import_walls_every_unique_name_and_covers_every_line(setup):
    s = setup
    r = _import(s, s.team, MESSY_LIST)
    assert r.status_code == 200
    body = r.json()

    # Coverage invariant: lines in == states out, one state per line, and
    # the mapping echoes every source line by number.
    lines = body["lines"]
    assert [ln["line"] for ln in lines] == [1, 2, 3, 4, 5, 6, 7]
    counts = body["counts"]
    assert counts["lines_in"] == 7
    assert sum(counts[st] for st in ci.STATES) == 7
    for ln in lines:
        assert ln["state"] in ci.STATES

    by_line = {ln["line"]: ln for ln in lines}
    assert by_line[1]["state"] == "skipped_header"
    assert by_line[1]["name"] == "Company"
    assert by_line[2]["state"] == "walled_provisional"
    assert by_line[2]["name_norm"] == "acme capital"
    assert by_line[3]["state"] == "skipped_empty"
    assert by_line[4]["state"] == "walled_provisional"
    assert by_line[4]["name_norm"] == "globex"       # quoted cell, LLC folded
    assert by_line[5]["state"] == "duplicate"        # same norm as line 2
    assert by_line[6]["state"] == "walled_provisional"
    assert by_line[6]["name"] == "Weird Co"          # control chars stripped
    assert by_line[7]["state"] == "skipped_empty"    # normalized to nothing

    # One provisional wall per unique name, walled-by-default for ALL members.
    walls = _walls(s.db, s.team)
    assert {w.subject_name_norm for w in walls} == \
        {"acme capital", "globex", "weird"}
    for w in walls:
        assert w.subject_company_id is None
        assert w.excluded_user_ids == "[]"
        assert w.reason == ci.PROVISIONAL_REASON
        assert w.created_by == s.a.id

    # The review mapping resolves matches: acme via identity, globex too.
    assert by_line[2]["matched_companies"] == \
        [{"id": s.acme.id, "name": "Acme Capital"}]
    assert by_line[4]["matched_companies"] == \
        [{"id": s.globex.id, "name": "Globex"}]
    assert by_line[6]["matched_companies"] == []


def test_header_token_after_first_content_line_is_walled_not_skipped(setup):
    s = setup
    r = _import(s, s.team, "Acme Capital\nClient\n")
    states = {ln["line"]: ln["state"] for ln in r.json()["lines"]}
    # Only the FIRST content line can be an "obvious header"; a later literal
    # "Client" is a name and gets walled (over-walling is the safe direction).
    assert states == {1: "walled_provisional", 2: "walled_provisional"}


# ── (b) provisional wall enforces immediately, before any review ─────────────

def test_provisional_wall_enforces_before_confirmation(setup):
    s = setup
    # Sanity: both members currently see Acme on the team plane.
    body = s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb).json()
    assert "Acme Capital" in {row["company_name"] for row in body["accounts"]}

    assert _import(s, s.team, "Acme Capital\n").status_code == 200

    # No confirm, no review — the name-wall already erases Acme for everyone,
    # in both directions (list, paths, search), matched via CompanyIdentity.
    for headers in (s.ha, s.hb):
        body = s.client.get(f"/api/teams/{s.team}/accounts",
                            headers=headers).json()
        assert {row["company_name"] for row in body["accounts"]} == {"Globex"}
        assert s.client.get(
            f"/api/teams/{s.team}/companies/{s.acme.id}/paths",
            headers=headers).status_code == 404
        assert s.client.get(f"/api/teams/{s.team}/search",
                            params={"q": "acme"},
                            headers=headers).json()["results"] == []


# ── (c) confirm: narrow single matches, keep ambiguity walled, go live ───────

def test_confirm_converts_single_match_keeps_multi_match_flips_live(setup):
    s = setup
    tid = _strict_team(s)
    r = _import(s, tid, "Acme Capital\nMeridian\nUnknown Ventures\n")
    assert r.status_code == 200
    by_norm = {ln["name_norm"]: ln for ln in r.json()["lines"]}
    assert len(by_norm["acme capital"]["matched_companies"]) == 1
    # Multi-match through the live-Company scan (suffixes fold to "meridian").
    assert {c["id"] for c in by_norm["meridian"]["matched_companies"]} == \
        {s.mer1.id, s.mer2.id}
    assert by_norm["unknown ventures"]["matched_companies"] == []

    # GET review mapping shows the same three provisional walls.
    rev = s.client.get(f"/api/teams/{tid}/conflicts", headers=s.ha).json()
    assert rev["view_state"] == "pending"
    assert {c["name_norm"] for c in rev["conflicts"]} == \
        {"acme capital", "meridian", "unknown ventures"}

    # Confirm requires the explicit flag.
    assert s.client.post(f"/api/teams/{tid}/conflicts/confirm",
                         json={"confirmed": False},
                         headers=s.ha).status_code == 400

    r = s.client.post(f"/api/teams/{tid}/conflicts/confirm",
                      json={"confirmed": True}, headers=s.ha)
    assert r.status_code == 200
    assert r.json() == {"team_id": tid, "view_state": "live",
                        "converted": 1, "kept_name_walls": 2}

    walls = {w.subject_name_norm: w for w in _walls(s.db, tid)}
    # Single match -> entity wall, name_norm KEPT (belt-and-braces).
    assert walls["acme capital"].subject_company_id == s.acme.id
    assert walls["acme capital"].subject_name_norm == "acme capital"
    # Ambiguous and unmatched stay name-walls — still enforcing.
    assert walls["meridian"].subject_company_id is None
    assert walls["unknown ventures"].subject_company_id is None

    # Interlock opened: pending -> live.
    s.db.expire_all()
    assert s.db.get(models.Team, tid).view_state == "live"
    body = s.client.get(f"/api/teams/{tid}/accounts", headers=s.hb).json()
    assert body["view_state"] == "live"
    # And the walls (entity AND kept name-walls) enforce on the live view.
    assert "Acme Capital" not in {row["company_name"]
                                  for row in body["accounts"]}


# ── (d) skip: audited bypass, reason mandatory ───────────────────────────────

def test_skip_requires_reason_then_flips_live_and_audits(setup):
    s = setup
    tid = _strict_team(s)

    for bad in ({"reason": ""}, {"reason": "   "}, {}):
        assert s.client.post(f"/api/teams/{tid}/conflicts/skip",
                             json=bad, headers=s.ha).status_code == 400
    s.db.expire_all()
    assert s.db.get(models.Team, tid).view_state == "pending"

    r = s.client.post(f"/api/teams/{tid}/conflicts/skip",
                      json={"reason": "solo practice, no screening duty"},
                      headers=s.ha)
    assert r.status_code == 200 and r.json()["view_state"] == "live"
    s.db.expire_all()
    assert s.db.get(models.Team, tid).view_state == "live"

    rows = _audit_rows(s.db, tid, "conflicts_skipped")
    assert len(rows) == 1 and rows[0].actor_user_id == s.a.id
    detail = json.loads(rows[0].detail_json)
    assert detail["reason"] == "solo practice, no screening duty"
    assert detail["view_state"] == ["pending", "live"]


# ── (e) idempotent re-import ─────────────────────────────────────────────────

def test_reimport_is_idempotent(setup):
    s = setup
    assert _import(s, s.team, MESSY_LIST).status_code == 200
    n_walls = len(_walls(s.db, s.team))

    r = _import(s, s.team, MESSY_LIST)
    assert r.status_code == 200
    counts = r.json()["counts"]
    # Every previously-walled name is now a duplicate; nothing new, nothing
    # dropped, coverage still exact.
    assert counts["walled_provisional"] == 0
    assert counts["duplicate"] == 4      # 3 unique names + the in-batch dup
    assert counts["lines_in"] == sum(counts[st] for st in ci.STATES)
    assert len(_walls(s.db, s.team)) == n_walls
    # Two imports, two audit rows: idempotence is about walls, not the trail.
    assert len(_audit_rows(s.db, s.team, "conflicts_imported")) == 2


# ── (f) access discipline: admin-only verbs, existence-hiding 404s ───────────

def test_non_admin_403_and_non_member_404_on_every_conflict_route(setup):
    s = setup
    calls = [
        ("post", f"/api/teams/{s.team}/conflicts/import", {"text": "Acme"}),
        ("get", f"/api/teams/{s.team}/conflicts", None),
        ("post", f"/api/teams/{s.team}/conflicts/confirm",
         {"confirmed": True}),
        ("post", f"/api/teams/{s.team}/conflicts/skip", {"reason": "x"}),
    ]
    for method, url, body in calls:
        kwargs = {"headers": s.hb}
        if body is not None:
            kwargs["json"] = body
        assert getattr(s.client, method)(url, **kwargs).status_code == 403, url
        kwargs["headers"] = s.ho
        assert getattr(s.client, method)(url, **kwargs).status_code == 404, url
    # And nothing leaked: no walls, no audit rows, view untouched.
    assert _walls(s.db, s.team) == []
    assert _audit_rows(s.db, s.team, "conflicts_imported") == []


def test_empty_import_text_is_rejected(setup):
    s = setup
    for text in ("", "   ", "\n\n"):
        assert _import(s, s.team, text).status_code == 400
    assert _walls(s.db, s.team) == []


# ── (g) audit rows for import / confirm / skip ───────────────────────────────

def test_audit_rows_for_import_and_confirm(setup):
    s = setup
    tid = _strict_team(s)
    _import(s, tid, "Company\nAcme Capital\nMeridian\n\n")
    s.client.post(f"/api/teams/{tid}/conflicts/confirm",
                  json={"confirmed": True}, headers=s.ha)

    imported = _audit_rows(s.db, tid, "conflicts_imported")
    assert len(imported) == 1 and imported[0].actor_user_id == s.a.id
    detail = json.loads(imported[0].detail_json)
    assert detail["lines_in"] == 4
    assert detail["walled_provisional"] == 2
    assert detail["skipped_header"] == 1 and detail["skipped_empty"] == 1

    confirmed = _audit_rows(s.db, tid, "conflicts_confirmed")
    assert len(confirmed) == 1 and confirmed[0].actor_user_id == s.a.id
    detail = json.loads(confirmed[0].detail_json)
    assert detail["converted"] == 1 and detail["kept_name_walls"] == 1
    assert detail["view_state"] == ["pending", "live"]
