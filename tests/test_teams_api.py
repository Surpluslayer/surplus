"""
Tests for the team plane (routes/teams.py + agents/relationship/team_view.py).

The confidentiality semantics ARE the product here (docs/
accounts-architecture.md §6), so these tests are less "does the endpoint
respond" and more "prove each gate": Level-1-shape-only serialization,
bidirectional ethical walls (including the all-members and name_norm forms),
the per-user kill switch, owner private accounts, the strict-pending
interlock, instant departures, and non-member existence-hiding.

Fixture style follows tests/test_book.py / test_relationships_api.py
(in-memory SQLite + real ORM rows, no LLM key so every band is
deterministic), but goes through a real TestClient app with real Session
rows and Bearer auth — the auth dependency and 403/404 discipline are part
of what's under test. Rows are built directly (no company_resolve import).
"""
from __future__ import annotations

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
from backend.db import Base, get_db
from backend.routes import teams as teams_route

LEVEL1_KEYS = {"member_name", "contact_name", "contact_title",
               "warmth_band", "last_touch_band"}
WARMTH_VALUES = {"active", "warm", "cooling", "dormant"}
TOUCH_VALUES = {"this week", "this month", "older", "never"}


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Deterministic warmth bands (no LLM), stable invite signatures.
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
    # The orchestrator registers the router in main.py; here we mount it on a
    # bare app so the test exercises routes/teams.py in isolation.
    app = FastAPI()
    app.include_router(teams_route.router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


# ── row builders (direct, no company_resolve) ───────────────────────────────

def _user(db, name, email):
    u = models.User(name=name, email=email)
    db.add(u)
    db.commit()
    return u


def _auth(db, user):
    """Real Session row + Bearer header, so current_user runs for real."""
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
          email=None, status="linked", is_current=True):
    """One person the user knows at the company: Contact + current linked
    AccountMembership + (lazily) the owner's Account row, plus an interaction
    that sets last-touch. The interaction carries deliberately radioactive
    content so leak assertions have something concrete to look for."""
    contact = models.Contact(
        user_id=user.id,
        primary_identity_key=f"li:{contact_name.lower().replace(' ', '-')}-{user.id}",
        name=contact_name, title=title, email=email,
    )
    db.add(contact)
    db.flush()
    db.add(models.AccountMembership(
        user_id=user.id, contact_id=contact.id, company_id=company.id,
        role_title=title, is_current=is_current, status=status,
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
    """Two users in one team, each with their own contacts/companies/edges:
      A (Daniel):    Jane @ Acme (3d ago, active) ; Gil @ Globex (60d, warm)
      B (Cofounder): Kate @ Acme (100d, dormant)  ; Nate @ Globex (never)
    Plus an outsider with a valid session but no membership."""
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


def _accounts(s, headers):
    r = s.client.get(f"/api/teams/{s.team}/accounts", headers=headers)
    assert r.status_code == 200
    return r.json()


def _paths(s, company, headers):
    return s.client.get(
        f"/api/teams/{s.team}/companies/{company.id}/paths", headers=headers)


def _search(s, q, headers):
    r = s.client.get(f"/api/teams/{s.team}/search",
                     params={"q": q}, headers=headers)
    assert r.status_code == 200
    return r.json()


def _company_names(payload):
    return {row["company_name"] for row in payload["accounts"]}


def _acct_row(payload, name):
    return next(r for r in payload["accounts"] if r["company_name"] == name)


# ── (a) merged view, Level-1 shape ONLY ─────────────────────────────────────

def test_both_members_see_merged_accounts(setup):
    for headers in (setup.ha, setup.hb):
        body = _accounts(setup, headers)
        assert body["view_state"] == "live"
        assert _company_names(body) == {"Acme Capital", "Globex"}
        acme = _acct_row(body, "Acme Capital")
        assert acme["member_count"] == 2 and acme["path_count"] == 2
        assert acme["warmth"] == "active"          # Jane (3d) wins the band
        globex = _acct_row(body, "Globex")
        assert globex["member_count"] == 2 and globex["path_count"] == 2
        assert globex["warmth"] == "warm"          # Gil (60d); Nate is never


def test_paths_emit_exactly_the_level1_shape(setup):
    r = _paths(setup, setup.acme, setup.hb)
    assert r.status_code == 200
    body = r.json()
    assert body["view_state"] == "live"
    assert body["company_name"] == "Acme Capital"
    assert len(body["paths"]) == 2
    for row in body["paths"]:
        # Exact key set: nothing beyond Level 1 can ever ride along.
        assert set(row) == LEVEL1_KEYS
        assert row["warmth_band"] in WARMTH_VALUES
        assert row["last_touch_band"] in TOUCH_VALUES
    by_contact = {p["contact_name"]: p for p in body["paths"]}
    assert by_contact["Jane Doe"]["member_name"] == "Daniel"
    assert by_contact["Jane Doe"]["contact_title"] == "Partner"
    assert by_contact["Jane Doe"]["warmth_band"] == "active"
    assert by_contact["Jane Doe"]["last_touch_band"] == "this week"
    assert by_contact["Kate Liu"]["member_name"] == "Cofounder"
    assert by_contact["Kate Liu"]["warmth_band"] == "dormant"
    assert by_contact["Kate Liu"]["last_touch_band"] == "older"


def test_never_touched_contact_bands(setup):
    body = _paths(setup, setup.globex, setup.ha).json()
    nate = next(p for p in body["paths"] if p["contact_name"] == "Nate Fox")
    assert nate["warmth_band"] == "dormant"
    assert nate["last_touch_band"] == "never"


def test_no_content_or_identifiers_leak_anywhere(setup):
    """Class C content (interaction titles/summaries), emails, and raw
    timestamps must not appear in ANY team response body."""
    blobs = [
        _paths(setup, setup.acme, setup.hb).text,
        _paths(setup, setup.globex, setup.hb).text,
        setup.client.get(f"/api/teams/{setup.team}/accounts",
                         headers=setup.hb).text,
        setup.client.get(f"/api/teams/{setup.team}/search",
                         params={"q": "a"}, headers=setup.hb).text,
    ]
    for blob in blobs:
        assert "SECRET-TITLE" not in blob and "SECRET-BODY" not in blob
        assert "jane@secret.example" not in blob and "@" not in blob
        assert "occurred_at" not in blob and "last_touch_at" not in blob
        assert str(_now().year) not in blob      # no raw dates of any shape


# ── (b) wall excluding one member: bidirectional erasure ────────────────────

def _wall(s, headers, **body):
    return s.client.post(f"/api/teams/{s.team}/walls", json=body,
                         headers=headers)


def test_wall_excluding_b_erases_company_for_b_and_bs_edges_for_a(setup):
    r = _wall(setup, setup.ha, company_id=setup.acme.id,
              excluded_user_ids=[setup.b.id], reason="conflict: opposing party")
    assert r.status_code == 200

    # Inbound: for B, Acme does not exist — list, paths, search, counts.
    body = _accounts(setup, setup.hb)
    assert _company_names(body) == {"Globex"}
    assert "Acme" not in str(body)
    assert _paths(setup, setup.acme, setup.hb).status_code == 404
    assert _search(setup, "acme", setup.hb)["results"] == []

    # Outbound: B's edges into Acme vanish from A's aggregates too.
    body_a = _accounts(setup, setup.ha)
    acme = _acct_row(body_a, "Acme Capital")
    assert acme["member_count"] == 1 and acme["path_count"] == 1
    paths_a = _paths(setup, setup.acme, setup.ha).json()["paths"]
    assert {p["member_name"] for p in paths_a} == {"Daniel"}
    assert "Kate Liu" not in str(paths_a)

    # B's untouched company is unaffected in both views.
    assert _paths(setup, setup.globex, setup.hb).status_code == 200


# ── (c) excluded_user_ids=[] means everyone ─────────────────────────────────

def test_wall_with_empty_exclusion_hides_company_from_all_members(setup):
    r = _wall(setup, setup.ha, company_id=setup.acme.id, reason="firm-wide")
    assert r.status_code == 200 and r.json()["excluded_user_ids"] == []
    for headers in (setup.ha, setup.hb):
        assert _company_names(_accounts(setup, headers)) == {"Globex"}
        assert _paths(setup, setup.acme, headers).status_code == 404
        assert _search(setup, "acme", headers)["results"] == []


# ── (d) name_norm wall (no company_id) ──────────────────────────────────────

def test_name_norm_wall_resolves_through_company_identity(setup):
    r = _wall(setup, setup.ha, name_norm="Globex",  # normalized by the route
              excluded_user_ids=[setup.b.id], reason="provisional import")
    assert r.status_code == 200 and r.json()["subject_company_id"] is None

    assert _company_names(_accounts(setup, setup.hb)) == {"Acme Capital"}
    assert _paths(setup, setup.globex, setup.hb).status_code == 404
    # A still sees Globex, but without walled-B's edge into it.
    paths_a = _paths(setup, setup.globex, setup.ha).json()["paths"]
    assert {p["member_name"] for p in paths_a} == {"Daniel"}


# ── (e) kill switch: edges leave the pool, viewing remains ──────────────────

def test_share_signals_false_removes_edges_but_keeps_viewing(setup):
    r = setup.client.patch(f"/api/teams/{setup.team}/members/me",
                           json={"share_signals": False}, headers=setup.hb)
    assert r.status_code == 200 and r.json()["share_signals"] is False

    # Nobody sees B's edges anymore — including B.
    for headers in (setup.ha, setup.hb):
        body = _accounts(setup, headers)
        assert body["view_state"] == "live"       # B can still view
        for name in ("Acme Capital", "Globex"):
            row = _acct_row(body, name)
            assert row["member_count"] == 1 and row["path_count"] == 1
        paths = _paths(setup, setup.acme, headers).json()["paths"]
        assert {p["member_name"] for p in paths} == {"Daniel"}

    # Flipping it back restores the edges (consent is revocable both ways).
    setup.client.patch(f"/api/teams/{setup.team}/members/me",
                       json={"share_signals": True}, headers=setup.hb)
    assert _acct_row(_accounts(setup, setup.ha),
                     "Acme Capital")["member_count"] == 2


# ── (f) owner sharing_level="private" hides only that owner's edges ─────────

def test_private_account_hides_only_that_owners_edges(setup):
    acct = (setup.db.query(models.Account)
            .filter(models.Account.owner_type == "user",
                    models.Account.owner_id == setup.a.id,
                    models.Account.company_id == setup.acme.id)
            .one())
    acct.sharing_level = "private"
    setup.db.commit()

    paths = _paths(setup, setup.acme, setup.hb).json()["paths"]
    assert {p["member_name"] for p in paths} == {"Cofounder"}
    assert "Jane Doe" not in str(paths)
    row = _acct_row(_accounts(setup, setup.hb), "Acme Capital")
    assert row["member_count"] == 1 and row["path_count"] == 1
    # A's Globex account is untouched: private is per-account, not per-user.
    assert _acct_row(_accounts(setup, setup.hb),
                     "Globex")["member_count"] == 2


# ── (g) strict + pending interlock ──────────────────────────────────────────

def test_strict_pending_blanks_every_relationship_read_until_live(db, client, setup):
    r = client.post("/api/teams",
                    json={"name": "Firm", "compliance_profile": "strict"},
                    headers=setup.ha)
    assert r.status_code == 200 and r.json()["view_state"] == "pending"
    tid = r.json()["team_id"]
    tok = client.post(f"/api/teams/{tid}/invite",
                      headers=setup.ha).json()["invite_token"]
    client.post(f"/api/teams/{tid}/join", json={"invite_token": tok},
                headers=setup.hb)

    pending = {"view_state": "pending"}
    assert client.get(f"/api/teams/{tid}/accounts",
                      headers=setup.hb).json() == pending
    assert client.get(f"/api/teams/{tid}/companies/{setup.acme.id}/paths",
                      headers=setup.hb).json() == pending
    assert client.get(f"/api/teams/{tid}/search", params={"q": "acme"},
                      headers=setup.hb).json() == pending

    # Non-admin cannot open the interlock.
    assert client.patch(f"/api/teams/{tid}", json={"view_state": "live"},
                        headers=setup.hb).status_code == 403
    # Admin flipping view_state to "live" is the conflict-import-done unlock.
    r = client.patch(f"/api/teams/{tid}", json={"view_state": "live"},
                     headers=setup.ha)
    assert r.status_code == 200 and r.json()["view_state"] == "live"
    body = client.get(f"/api/teams/{tid}/accounts", headers=setup.hb).json()
    assert body["view_state"] == "live"
    assert {r_["company_name"] for r_ in body["accounts"]} == \
        {"Acme Capital", "Globex"}


# ── (h) leaving removes edges instantly ─────────────────────────────────────

def test_leave_removes_members_edges_immediately(setup):
    r = setup.client.delete(f"/api/teams/{setup.team}/members/me",
                            headers=setup.hb)
    assert r.status_code == 200
    # Query-time assembly: the very next read has no trace of B.
    body = _accounts(setup, setup.ha)
    for name in ("Acme Capital", "Globex"):
        assert _acct_row(body, name)["member_count"] == 1
    paths = _paths(setup, setup.acme, setup.ha).json()["paths"]
    assert {p["member_name"] for p in paths} == {"Daniel"}
    # And the departed member is an outsider again (existence-hiding 404).
    assert setup.client.get(f"/api/teams/{setup.team}/accounts",
                            headers=setup.hb).status_code == 404


# ── (i) non-member gets 403/404 everywhere ──────────────────────────────────

def test_non_member_is_denied_on_every_team_endpoint(setup):
    c, t, ho = setup.client, setup.team, setup.ho
    checks = [
        c.get(f"/api/teams/{t}/accounts", headers=ho),
        c.get(f"/api/teams/{t}/companies/{setup.acme.id}/paths", headers=ho),
        c.get(f"/api/teams/{t}/search", params={"q": "acme"}, headers=ho),
        c.get(f"/api/teams/{t}/walls", headers=ho),
        c.post(f"/api/teams/{t}/walls",
               json={"company_id": setup.acme.id}, headers=ho),
        c.delete(f"/api/teams/{t}/walls/1", headers=ho),
        c.post(f"/api/teams/{t}/invite", headers=ho),
        c.patch(f"/api/teams/{t}", json={"view_state": "live"}, headers=ho),
        c.patch(f"/api/teams/{t}/members/me",
                json={"share_signals": False}, headers=ho),
        c.delete(f"/api/teams/{t}/members/me", headers=ho),
    ]
    for r in checks:
        assert r.status_code in (403, 404), r.url
    # /mine simply doesn't list it.
    assert c.get("/api/teams/mine", headers=ho).json()["teams"] == []


def test_member_but_not_admin_gets_403_on_admin_verbs(setup):
    c, t, hb = setup.client, setup.team, setup.hb
    assert c.post(f"/api/teams/{t}/invite", headers=hb).status_code == 403
    assert c.get(f"/api/teams/{t}/walls", headers=hb).status_code == 403
    assert c.post(f"/api/teams/{t}/walls",
                  json={"company_id": setup.acme.id},
                  headers=hb).status_code == 403
    assert c.patch(f"/api/teams/{t}",
                   json={"compliance_profile": "strict"},
                   headers=hb).status_code == 403


# ── (j) invite tokens ───────────────────────────────────────────────────────

def test_invite_token_join_works_and_garbage_is_rejected(setup):
    c, t = setup.client, setup.team
    # Garbage / malformed / forged tokens are rejected.
    for bad in ("nope", "1.2", f"{t}.99999999999.deadbeef" + "0" * 24,
                "999.99999999999.deadbeef"):
        r = c.post(f"/api/teams/{t}/join", json={"invite_token": bad},
                   headers=setup.ho)
        assert r.status_code == 403, bad
    # A token minted for ANOTHER team does not open this one.
    r2 = c.post("/api/teams", json={"name": "Other"}, headers=setup.ha)
    other_tok = c.post(f"/api/teams/{r2.json()['team_id']}/invite",
                       headers=setup.ha).json()["invite_token"]
    assert c.post(f"/api/teams/{t}/join",
                  json={"invite_token": other_tok},
                  headers=setup.ho).status_code == 403
    # A valid token admits the outsider, who then sees the (gated) view.
    tok = c.post(f"/api/teams/{t}/invite",
                 headers=setup.ha).json()["invite_token"]
    r = c.post(f"/api/teams/{t}/join", json={"invite_token": tok},
               headers=setup.ho)
    assert r.status_code == 200 and r.json()["role"] == "member"
    assert c.get(f"/api/teams/{t}/accounts",
                 headers=setup.ho).status_code == 200
    mine = c.get("/api/teams/mine", headers=setup.ho).json()["teams"]
    assert [m["team_id"] for m in mine] == [t]


# ── extra gate hygiene: only current+linked edges contribute ────────────────

def test_pending_review_and_past_edges_do_not_contribute(setup):
    _edge(setup.db, setup.b, setup.acme, "Pia Old", "Advisor",
          days_since=5, is_current=False)
    _edge(setup.db, setup.b, setup.acme, "Rex Maybe", "Analyst",
          days_since=5, status="pending_review")
    paths = _paths(setup, setup.acme, setup.ha).json()["paths"]
    names = {p["contact_name"] for p in paths}
    assert "Pia Old" not in names and "Rex Maybe" not in names
    assert names == {"Jane Doe", "Kate Liu"}


def test_members_roster(setup):
    """Any member sees the roster (names + roles only); non-members get the
    same 404 as every other team surface; no relationship data in the shape."""
    s = setup
    r = s.client.get(f"/api/teams/{s.team}/members", headers=s.hb)
    assert r.status_code == 200
    members = r.json()["members"]
    assert [(m["name"], m["role"]) for m in members] == [
        ("Daniel", "admin"), ("Cofounder", "member")]
    assert set(members[0]) == {"user_id", "name", "role", "share_signals"}
    assert s.client.get(f"/api/teams/{s.team}/members",
                        headers=s.ho).status_code == 404
