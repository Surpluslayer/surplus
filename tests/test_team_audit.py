"""
Tests for the team-plane audit trail (models.TeamAuditLog +
agents/relationship/audit.py, wired through routes/teams.py).

The trail is the law-firm compliance product surface (docs/
accounts-architecture.md §6: "who viewed which aggregate, when" plus every
wall/policy change), so these tests prove the two deliberately different
failure modes end to end:

  * Mutations (walls, policy, membership) write their audit row IN the same
    transaction — if the audit write fails, the change itself must not
    commit (an unaudited wall change is impossible).
  * Reads (accounts/paths/search, and viewing the audit log itself) are
    best-effort — a broken audit path must never 500 a view.

Plus: the admin-only audit endpoint (member 403 / non-member 404, paged,
newest first, event filter), and the no-content invariant — read-event rows
carry counts and the viewer's own query string, never relationship content.

Fixture style copied from tests/test_teams_api.py (in-memory SQLite, real
TestClient + Bearer auth), with raise_server_exceptions=False so the atomic
failure mode is observable as a 500 rather than a test-side exception.
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
from backend.agents.relationship import audit as team_audit
from backend.db import Base, get_db
from backend.routes import teams as teams_route

ENTRY_KEYS = {"id", "at", "actor", "event", "company", "detail"}


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
    app = FastAPI()
    app.include_router(teams_route.router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    # raise_server_exceptions=False: the atomicity test needs to observe the
    # 500 a failed in-transaction audit write produces, then inspect the DB.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── row builders (same pattern as test_teams_api.py) ────────────────────────

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


def _company(db, name, norm):
    c = models.Company(canonical_name=name)
    db.add(c)
    db.flush()
    db.add(models.CompanyIdentity(company_id=c.id, kind="name_norm",
                                  value=norm))
    db.commit()
    return c


def _edge(db, user, company, contact_name, title, days_since=None,
          email=None):
    """One person the user knows at the company, with deliberately
    radioactive interaction content so the no-content assertions have
    something concrete to look for."""
    contact = models.Contact(
        user_id=user.id,
        primary_identity_key=f"li:{contact_name.lower().replace(' ', '-')}-{user.id}",
        name=contact_name, title=title, email=email,
    )
    db.add(contact)
    db.flush()
    db.add(models.AccountMembership(
        user_id=user.id, contact_id=contact.id, company_id=company.id,
        role_title=title, is_current=True, status="linked",
    ))
    acct = (db.query(models.Account)
            .filter(models.Account.owner_type == "user",
                    models.Account.owner_id == user.id,
                    models.Account.company_id == company.id)
            .first())
    if acct is None:
        db.add(models.Account(owner_type="user", owner_id=user.id,
                              company_id=company.id))
    if days_since is not None:
        db.add(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id,
            source_type="manual_note", interaction_type="note",
            occurred_at=_now() - timedelta(days=days_since),
            title="SECRET-TITLE coffee downtown",
            summary="SECRET-BODY they are raising a fund",
        ))
    db.commit()
    return contact


@pytest.fixture
def setup(db, client):
    """Same topology as test_teams_api.py: A (admin) and B (member) share a
    team; both know people at Acme and Globex; an outsider holds a valid
    session but no membership."""
    a = _user(db, "Daniel", "daniel@example.com")
    b = _user(db, "Cofounder", "cof@example.com")
    o = _user(db, "Outsider", "out@example.com")
    ha, hb, ho = _auth(db, a), _auth(db, b), _auth(db, o)

    acme = _company(db, "Acme Capital", "acme capital")
    globex = _company(db, "Globex", "globex")

    _edge(db, a, acme, "Jane Doe", "Partner", days_since=3,
          email="jane@secret.example")
    _edge(db, a, globex, "Gil Ortiz", "CTO", days_since=60)
    _edge(db, b, acme, "Kate Liu", "Principal", days_since=100)
    _edge(db, b, globex, "Nate Fox", "COO", days_since=None)

    r = client.post("/api/teams", json={"name": "Surplus"}, headers=ha)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    team_id = r.json()["team_id"]
    tok = client.post(f"/api/teams/{team_id}/invite",
                      headers=ha).json()["invite_token"]
    r = client.post(f"/api/teams/{team_id}/join",
                    json={"invite_token": tok}, headers=hb)
    assert r.status_code == 200 and r.json()["role"] == "member"

    return SimpleNamespace(db=db, client=client, a=a, b=b, o=o,
                           ha=ha, hb=hb, ho=ho, team=team_id,
                           acme=acme, globex=globex)


# ── helpers ──────────────────────────────────────────────────────────────────

def _rows(db, team_id, event=None):
    """Raw TeamAuditLog rows for the team, oldest first."""
    q = (db.query(models.TeamAuditLog)
         .filter(models.TeamAuditLog.team_id == team_id))
    if event is not None:
        q = q.filter(models.TeamAuditLog.event == event)
    return q.order_by(models.TeamAuditLog.id).all()


def _only(db, team_id, event):
    rows = _rows(db, team_id, event)
    assert len(rows) == 1, f"expected exactly one {event} row, got {len(rows)}"
    return rows[0]


def _detail(row):
    return json.loads(row.detail_json or "{}")


def _get_audit(s, headers, **params):
    return s.client.get(f"/api/teams/{s.team}/audit",
                        params=params, headers=headers)


def _boom(*args, **kwargs):
    raise RuntimeError("audit backend down")


# ── (a) mutations write rows atomically ─────────────────────────────────────

def test_wall_create_and_delete_write_audit_rows(setup):
    s = setup
    r = s.client.post(
        f"/api/teams/{s.team}/walls",
        json={"company_id": s.acme.id, "excluded_user_ids": [s.b.id],
              "reason": "conflict: opposing party"},
        headers=s.ha)
    assert r.status_code == 200
    wall_id = r.json()["wall_id"]

    row = _only(s.db, s.team, "wall_created")
    assert row.actor_user_id == s.a.id
    assert row.subject_company_id == s.acme.id
    d = _detail(row)
    assert d["wall_id"] == wall_id
    assert d["excluded_user_ids"] == [s.b.id]
    assert d["reason"] == "conflict: opposing party"

    r = s.client.delete(f"/api/teams/{s.team}/walls/{wall_id}", headers=s.ha)
    assert r.status_code == 200
    row = _only(s.db, s.team, "wall_deleted")
    assert row.actor_user_id == s.a.id
    assert row.subject_company_id == s.acme.id
    assert _detail(row)["wall_id"] == wall_id


def test_name_norm_wall_audit_carries_the_norm(setup):
    s = setup
    r = s.client.post(f"/api/teams/{s.team}/walls",
                      json={"name_norm": "Globex", "reason": "provisional"},
                      headers=s.ha)
    assert r.status_code == 200
    row = _only(s.db, s.team, "wall_created")
    assert row.subject_company_id is None
    d = _detail(row)
    assert d["name_norm"] == "globex"          # normalized by the route
    assert d["excluded_user_ids"] == []        # empty = all members


def test_failed_audit_write_rolls_back_the_wall(setup, monkeypatch):
    """Atomicity: if the audit row cannot be written, the wall change itself
    must not commit — an unaudited wall change is impossible."""
    s = setup
    monkeypatch.setattr(team_audit, "write", _boom)
    r = s.client.post(f"/api/teams/{s.team}/walls",
                      json={"company_id": s.acme.id, "reason": "x"},
                      headers=s.ha)
    assert r.status_code == 500
    s.db.rollback()                            # drop the aborted transaction
    assert s.db.query(models.Wall).count() == 0
    assert _rows(s.db, s.team, "wall_created") == []


def test_lifecycle_and_policy_events_are_audited(setup):
    s = setup
    # setup already exercised create + join.
    row = _only(s.db, s.team, "team_created")
    assert row.actor_user_id == s.a.id
    assert _detail(row)["name"] == "Surplus"
    row = _only(s.db, s.team, "member_joined")
    assert row.actor_user_id == s.b.id and _detail(row)["role"] == "member"

    r = s.client.patch(f"/api/teams/{s.team}/members/me",
                       json={"share_signals": False}, headers=s.hb)
    assert r.status_code == 200
    row = _only(s.db, s.team, "share_signals_changed")
    assert row.actor_user_id == s.b.id
    assert _detail(row) == {"old": True, "new": False}

    r = s.client.patch(f"/api/teams/{s.team}",
                       json={"compliance_profile": "strict",
                             "view_state": "pending"}, headers=s.ha)
    assert r.status_code == 200
    row = _only(s.db, s.team, "profile_changed")
    assert _detail(row) == {"old": "collaborative", "new": "strict"}
    row = _only(s.db, s.team, "view_state_changed")
    assert _detail(row) == {"old": "live", "new": "pending"}

    r = s.client.delete(f"/api/teams/{s.team}/members/me", headers=s.hb)
    assert r.status_code == 200
    row = _only(s.db, s.team, "member_left")
    assert row.actor_user_id == s.b.id


# ── (b) reads write best-effort rows with counts ────────────────────────────

def test_view_accounts_paths_and_search_write_read_rows(setup):
    s = setup
    r = s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    assert r.status_code == 200
    row = _only(s.db, s.team, "view_accounts")
    assert row.actor_user_id == s.b.id
    assert _detail(row) == {"companies": 2}

    r = s.client.get(f"/api/teams/{s.team}/companies/{s.acme.id}/paths",
                     headers=s.hb)
    assert r.status_code == 200
    row = _only(s.db, s.team, "view_paths")
    assert row.actor_user_id == s.b.id
    assert row.subject_company_id == s.acme.id
    assert _detail(row) == {"rows": 2}

    r = s.client.get(f"/api/teams/{s.team}/search",
                     params={"q": "acme"}, headers=s.hb)
    assert r.status_code == 200
    row = _only(s.db, s.team, "search")
    assert row.actor_user_id == s.b.id
    assert _detail(row) == {"query": "acme", "hits": 1}


# ── (c) the audit endpoint ───────────────────────────────────────────────────

def test_audit_endpoint_is_admin_only(setup):
    s = setup
    assert _get_audit(s, s.hb).status_code == 403    # member: known, denied
    assert _get_audit(s, s.ho).status_code == 404    # outsider: hidden
    assert _get_audit(s, s.ha).status_code == 200    # admin


def test_audit_endpoint_shape_paging_order_and_filter(setup):
    s = setup
    # Generate a mix of events on top of setup's team_created/member_joined.
    s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    s.client.get(f"/api/teams/{s.team}/companies/{s.acme.id}/paths",
                 headers=s.hb)
    s.client.post(f"/api/teams/{s.team}/walls",
                  json={"company_id": s.acme.id, "reason": "conflict"},
                  headers=s.ha)

    r = _get_audit(s, s.ha)
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 100 and body["offset"] == 0
    assert body["total"] == len(body["entries"]) == 5
    ids = [e["id"] for e in body["entries"]]
    assert ids == sorted(ids, reverse=True)          # newest first

    for e in body["entries"]:
        assert set(e) == ENTRY_KEYS
        assert isinstance(e["detail"], dict)
        assert e["actor"] is not None and set(e["actor"]) == {"user_id", "name"}
    by_event = {e["event"]: e for e in body["entries"]}
    assert by_event["wall_created"]["actor"]["name"] == "Daniel"
    assert by_event["wall_created"]["company"] == {
        "id": s.acme.id, "name": "Acme Capital"}
    assert by_event["view_accounts"]["actor"]["name"] == "Cofounder"
    assert by_event["view_accounts"]["company"] is None

    # Paging: page over an event-filtered stream, because every audit view
    # appends its own audit_viewed row and would shift unfiltered offsets
    # between requests. Three view_accounts events, pages of two: no overlap,
    # newest first, stable total.
    s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    p1 = _get_audit(s, s.ha, event="view_accounts", limit=2, offset=0).json()
    p2 = _get_audit(s, s.ha, event="view_accounts", limit=2, offset=2).json()
    assert p1["total"] == p2["total"] == 3
    assert len(p1["entries"]) == 2 and len(p2["entries"]) == 1
    assert min(e["id"] for e in p1["entries"]) > \
        max(e["id"] for e in p2["entries"])
    assert {e["event"] for e in p1["entries"] + p2["entries"]} == \
        {"view_accounts"}

    # Limit is clamped to 500.
    assert _get_audit(s, s.ha, limit=9999).json()["limit"] == 500


def test_viewing_the_audit_log_is_itself_audited(setup):
    s = setup
    assert _get_audit(s, s.ha, event="wall_created").status_code == 200
    row = _only(s.db, s.team, "audit_viewed")
    assert row.actor_user_id == s.a.id
    d = _detail(row)
    assert d["event"] == "wall_created" and d["rows"] == 0
    # The next view of the trail surfaces the previous audit_viewed row.
    body = _get_audit(s, s.ha).json()
    assert "audit_viewed" in {e["event"] for e in body["entries"]}


# ── (e) no relationship content in any audit row ────────────────────────────

def test_read_audit_rows_carry_counts_never_content(setup):
    s = setup
    s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    s.client.get(f"/api/teams/{s.team}/companies/{s.acme.id}/paths",
                 headers=s.hb)
    s.client.get(f"/api/teams/{s.team}/search",
                 params={"q": "acme"}, headers=s.hb)
    _get_audit(s, s.ha)

    rows = _rows(s.db, s.team)
    assert rows, "expected audit rows"
    radioactive = ("SECRET-TITLE", "SECRET-BODY", "jane@secret.example",
                   "Jane Doe", "Kate Liu", "Gil Ortiz", "Nate Fox")
    for row in rows:
        for marker in radioactive:
            assert marker not in (row.detail_json or ""), \
                f"{marker!r} leaked into {row.event} detail_json"
    # Read rows are counts (+ the viewer's own query string) only.
    for event, keys in (("view_accounts", {"companies"}),
                        ("view_paths", {"rows"}),
                        ("search", {"query", "hits"})):
        assert set(_detail(_only(s.db, s.team, event))) == keys


# ── (f) a broken audit path never 500s a view ───────────────────────────────

def test_failed_audit_write_does_not_break_reads(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(team_audit, "write", _boom)

    r = s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    assert r.status_code == 200
    assert {row["company_name"] for row in r.json()["accounts"]} == \
        {"Acme Capital", "Globex"}
    r = s.client.get(f"/api/teams/{s.team}/companies/{s.acme.id}/paths",
                     headers=s.hb)
    assert r.status_code == 200 and len(r.json()["paths"]) == 2
    r = s.client.get(f"/api/teams/{s.team}/search",
                     params={"q": "acme"}, headers=s.hb)
    assert r.status_code == 200 and len(r.json()["results"]) == 1
    # The audit log view is a read too: it must survive its own audit failing.
    assert _get_audit(s, s.ha).status_code == 200

    # Nothing was recorded (the write itself is what failed) and the session
    # is still healthy for the next request.
    assert _rows(s.db, s.team, "view_accounts") == []
    monkeypatch.undo()
    r = s.client.get(f"/api/teams/{s.team}/accounts", headers=s.hb)
    assert r.status_code == 200
    assert len(_rows(s.db, s.team, "view_accounts")) == 1
