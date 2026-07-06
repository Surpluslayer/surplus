"""
Route tests for the account layer read API (routes/accounts.py +
agents/relationship/accounts_read.py).

Repo convention : call route functions directly with an in-memory SQLAlchemy
session + real ORM rows (no TestClient / auth cookies), and force the
deterministic no-LLM health path like tests/test_book.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import accounts as acc_route
from backend.routes.accounts import AccountPatch, OverlayIn


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Force score_health onto the deterministic heuristic path.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(db, name="Owner", email="owner@x.com"):
    u = models.User(name=name, email=email)
    db.add(u); db.commit()
    return u


def _contact(db, user, name, key, title=None):
    c = models.Contact(user_id=user.id, primary_identity_key=key,
                       name=name, title=title)
    db.add(c); db.commit()
    return c


def _member(db, user, contact, company, **kw):
    m = models.AccountMembership(user_id=user.id, contact_id=contact.id,
                                 company_id=company.id, **kw)
    db.add(m); db.commit()
    return m


def _touch(db, user, contact, days_ago, title="Caught up", direction="inbound"):
    it = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        source_type="manual_note", interaction_type="note",
        direction=direction, occurred_at=_now() - timedelta(days=days_ago),
        title=title)
    db.add(it); db.commit()
    return it


def _seed(db):
    """One owner with an Acme account: alice touched 2d ago (active), bob 40d
    (cooling), cara 120d (dormant). Heuristic math (tier core, cadence 30,
    weight .8 -> eff 24): 2/24 active; 40/24>=1 cooling; 120d>=90 dormant."""
    u = _user(db)
    co = models.Company(canonical_name="Acme Corp", primary_domain="acme.com")
    db.add(co); db.commit()

    alice = _contact(db, u, "Alice Zhang", "li:alice", title="VP Eng")
    bob = _contact(db, u, "Bob Marsh", "li:bob", title="AE")
    cara = _contact(db, u, "Cara Diaz", "li:cara", title="CFO")
    for c in (alice, bob, cara):
        _member(db, u, c, co)

    acct = models.Account(owner_type="user", owner_id=u.id, company_id=co.id)
    db.add(acct); db.commit()

    _touch(db, u, alice, 2)
    _touch(db, u, alice, 30, title="Old note")
    _touch(db, u, bob, 40, direction="outbound")
    _touch(db, u, cara, 120)
    return u, co, acct, alice, bob, cara


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_returns_owned_accounts_only(db):
    u, co, acct, alice, *_ = _seed(db)
    out = acc_route.list_accounts(db=db, user=u)
    assert [a["id"] for a in out["accounts"]] == [acct.id]
    row = out["accounts"][0]
    assert row["company"]["canonical_name"] == "Acme Corp"
    assert row["tier"] == "tracked" and row["sharing_level"] == "metadata"
    # Preview is warmth (recency) ordered: alice first, cap 3.
    assert row["member_preview"][0] == "Alice Zhang"
    assert len(row["member_preview"]) == 3

    # A second user sees nothing — accounts never leak across owners.
    other = _user(db, name="Other", email="other@x.com")
    assert acc_route.list_accounts(db=db, user=other)["accounts"] == []


def test_list_filters_tier_and_q(db):
    u, co, acct, *_ = _seed(db)
    assert acc_route.list_accounts(tier="key", db=db, user=u)["accounts"] == []
    assert len(acc_route.list_accounts(q="acme", db=db, user=u)["accounts"]) == 1
    assert acc_route.list_accounts(q="zzz", db=db, user=u)["accounts"] == []
    # q also matches previewed member names.
    assert len(acc_route.list_accounts(q="alice", db=db, user=u)["accounts"]) == 1


def test_list_sorts_starred_then_strength(db):
    u, co, acct, *_ = _seed(db)
    co2 = models.Company(canonical_name="Beta LLC")
    db.add(co2); db.commit()
    acct2 = models.Account(owner_type="user", owner_id=u.id, company_id=co2.id,
                           starred=True)
    db.add(acct2); db.commit()
    acc_route.recompute_account(acct.id, db=db, user=u)   # acct gets strength
    out = acc_route.list_accounts(db=db, user=u)["accounts"]
    # Starred Beta first despite Acme's higher strength.
    assert [a["id"] for a in out] == [acct2.id, acct.id]


# ── detail ───────────────────────────────────────────────────────────────────

def test_detail_members_sorted_and_coverage(db):
    u, co, acct, alice, bob, cara = _seed(db)
    d = acc_route.get_account(acct.id, db=db, user=u)

    names = [m["name"] for m in d["members"]]
    assert names == ["Alice Zhang", "Bob Marsh", "Cara Diaz"]  # warmest first
    statuses = [m["health"]["status"] for m in d["members"]]
    assert statuses == ["active", "cooling", "dormant"]
    for m in d["members"]:
        assert set(m["health"]) >= {"status", "needs_outreach", "reason", "priority"}

    assert d["coverage"] == {"total": 3, "warm": 1, "cooling": 1,
                             "dormant": 1, "single_threaded": True}

    # Timeline is merged across members, newest first, and carries the name.
    tl = d["timeline"]
    assert len(tl) == 4
    assert tl[0]["contact_name"] == "Alice Zhang"
    occurred = [t["occurred_at"] for t in tl]
    assert occurred == sorted(occurred, reverse=True)


def test_detail_former_member_shows_new_company(db):
    u, co, acct, alice, *_ = _seed(db)
    newco = models.Company(canonical_name="NewCo")
    db.add(newco); db.commit()
    dan = _contact(db, u, "Dan Ito", "li:dan")
    _member(db, u, dan, co, is_current=False,
            started_at=_now() - timedelta(days=900),
            ended_at=_now() - timedelta(days=30))
    _member(db, u, dan, newco)      # current edge elsewhere

    d = acc_route.get_account(acct.id, db=db, user=u)
    assert [m["name"] for m in d["former_members"]] == ["Dan Ito"]
    assert d["former_members"][0]["now_at"] == "NewCo"
    # Former members never count toward coverage/members.
    assert d["coverage"]["total"] == 3


def test_detail_404_on_non_owned(db):
    u, co, acct, *_ = _seed(db)
    other = _user(db, name="Other", email="other@x.com")
    with pytest.raises(HTTPException) as e:
        acc_route.get_account(acct.id, db=db, user=other)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        acc_route.get_account(99999, db=db, user=u)
    assert e.value.status_code == 404


# ── patch ────────────────────────────────────────────────────────────────────

def test_patch_updates_fields(db):
    u, co, acct, *_ = _seed(db)
    out = acc_route.patch_account(
        acct.id,
        AccountPatch(tier="key", starred=True, objective="Intro to platform team",
                     notes="warm via Alice", sharing_level="private"),
        db=db, user=u)
    assert out["tier"] == "key" and out["starred"] is True
    assert out["objective"] == "Intro to platform team"
    assert out["notes"] == "warm via Alice"
    assert out["sharing_level"] == "private"
    # PATCH also refreshes the cached rollups.
    assert out["rollups"]["contact_count"] == 3


def test_patch_rejects_bad_sharing_level_and_tier(db):
    u, co, acct, *_ = _seed(db)
    with pytest.raises(HTTPException) as e:
        acc_route.patch_account(acct.id, AccountPatch(sharing_level="public"),
                                db=db, user=u)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        acc_route.patch_account(acct.id, AccountPatch(tier="platinum"),
                                db=db, user=u)
    assert e.value.status_code == 400
    # Nothing was written.
    db.refresh(acct)
    assert acct.sharing_level == "metadata" and acct.tier == "tracked"


def test_patch_404_on_non_owned(db):
    u, co, acct, *_ = _seed(db)
    other = _user(db, name="Other", email="other@x.com")
    with pytest.raises(HTTPException) as e:
        acc_route.patch_account(acct.id, AccountPatch(starred=True),
                                db=db, user=other)
    assert e.value.status_code == 404


# ── recompute ────────────────────────────────────────────────────────────────

def test_recompute_updates_rollups(db):
    u, co, acct, alice, bob, cara = _seed(db)
    assert acct.strength_score is None and acct.contact_count == 0

    out = acc_route.recompute_account(acct.id, db=db, user=u)
    r = out["rollups"]
    assert r["contact_count"] == 3
    assert r["warmest_contact_id"] == alice.id
    # Documented formula: mean of (100 - min(days,100)) = (98+60+0)/3.
    assert r["strength_score"] == pytest.approx(52.67, abs=0.5)
    # last_touch_at = alice's 2-days-ago interaction.
    assert r["last_touch_at"] is not None
    last = datetime.fromisoformat(r["last_touch_at"])
    assert abs((_now() - last).days - 2) <= 1


def test_recompute_empty_account_zeroes_out(db):
    u = _user(db)
    co = models.Company(canonical_name="Ghost Inc")
    db.add(co); db.commit()
    acct = models.Account(owner_type="user", owner_id=u.id, company_id=co.id,
                          contact_count=7, strength_score=88.0)
    db.add(acct); db.commit()
    out = acc_route.recompute_account(acct.id, db=db, user=u)
    r = out["rollups"]
    assert r["contact_count"] == 0
    assert r["strength_score"] is None and r["warmest_contact_id"] is None


# ── overlay ──────────────────────────────────────────────────────────────────

def test_overlay_rename_reflected_in_summary(db):
    u, co, acct, *_ = _seed(db)
    out = acc_route.upsert_overlay(acct.id, OverlayIn(canonical_name="Acme Health"),
                                   db=db, user=u)
    assert out["company"]["canonical_name"] == "Acme Health"
    # Global row untouched — the correction is per-viewer.
    db.refresh(co)
    assert co.canonical_name == "Acme Corp"
    # List reflects it too.
    row = acc_route.list_accounts(db=db, user=u)["accounts"][0]
    assert row["company"]["canonical_name"] == "Acme Health"


def test_overlay_rejected_hides_account(db):
    u, co, acct, *_ = _seed(db)
    out = acc_route.upsert_overlay(acct.id, OverlayIn(rejected=True),
                                   db=db, user=u)
    assert out == {"id": acct.id, "rejected": True}
    assert acc_route.list_accounts(db=db, user=u)["accounts"] == []
    with pytest.raises(HTTPException) as e:
        acc_route.get_account(acct.id, db=db, user=u)
    assert e.value.status_code == 404

    # Un-reject (upsert, not insert) brings it back.
    out = acc_route.upsert_overlay(acct.id, OverlayIn(rejected=False),
                                   db=db, user=u)
    assert out["company"]["canonical_name"] == "Acme Corp"
    assert (db.query(models.CompanyOverlay)
              .filter_by(user_id=u.id, company_id=co.id).count()) == 1


def test_overlay_404_on_non_owned(db):
    u, co, acct, *_ = _seed(db)
    other = _user(db, name="Other", email="other@x.com")
    with pytest.raises(HTTPException) as e:
        acc_route.upsert_overlay(acct.id, OverlayIn(rejected=True),
                                 db=db, user=other)
    assert e.value.status_code == 404


def test_list_lazily_heals_null_rollups(db):
    """Accounts born from a bulk backfill land with NULL rollups; the first
    list view must recompute them so the tab never renders empty chips."""
    u, co, acct, *_ = _seed(db)
    acct.strength_score = None
    acct.contact_count = 0
    db.commit()
    out = acc_route.list_accounts(db=db, user=u)
    row = next(a for a in out["accounts"] if a["id"] == acct.id)
    assert row["rollups"]["contact_count"] > 0
    assert row["rollups"]["strength_score"] is not None
    db.refresh(acct)
    assert acct.contact_count > 0


def test_list_paginates_and_reports_total(db):
    """The list is DB-paged: a big book returns only the page plus the full
    count, so the tab request stays cheap at any book size."""
    u, co, acct, *_ = _seed(db)
    for i in range(5):
        c = models.Company(canonical_name=f"Extra {i}")
        db.add(c); db.commit()
        db.add(models.Account(owner_type="user", owner_id=u.id,
                              company_id=c.id))
        db.commit()
    out = acc_route.list_accounts(limit=3, db=db, user=u)
    assert out["total"] == 6
    assert len(out["accounts"]) == 3
    page2 = acc_route.list_accounts(limit=3, offset=3, db=db, user=u)
    ids = {a["id"] for a in out["accounts"]} | {a["id"] for a in page2["accounts"]}
    assert len(ids) == 6
