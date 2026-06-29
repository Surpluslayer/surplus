"""
Tests for the CONTACT IDENTITY-RESOLUTION / MERGE layer (the ContactIdentity
table + agents/relationship/identity.py).

Covers:
  - the _migrate_contact_identities backfill lifts each Contact's email / phone /
    linkedin field into ContactIdentity rows (is_primary aligned to the contact's
    primary_identity_key);
  - find_duplicate_clusters merges (a) a shared email, (b) a shared linkedin id,
    (c) a BRIDGE record linking an email-only + a linkedin-only contact, and does
    NOT merge two different people who only share a first name;
  - merge_contacts reassigns prospects / interactions / facts / outgoing messages
    to the survivor, moves identities, deletes the duplicate, and is idempotent;
  - backfill_merge dry-run (apply=False) changes NOTHING; apply=True merges;
  - same name + company_domain is surfaced for REVIEW, never auto-merged.

Pattern mirrors test_email_accounts.py / test_contact_dedup.py : in-memory SQLite,
direct calls (no FastAPI app import).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.db as dbmod
from backend import models
from backend.db import Base
from backend.agents.relationship import identity as idy


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine, monkeypatch):
    # point the module-level ENGINE at our in-memory db so the migration runs here
    monkeypatch.setattr(dbmod, "ENGINE", engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Host"),
                    email=kw.get("email", "host@x.com"),
                    unipile_account_id=kw.get("acct", "li_acct_1"))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _contact(db, user, key, **kw):
    c = models.Contact(user_id=user.id, primary_identity_key=key, **kw)
    db.add(c); db.commit(); db.refresh(c)
    return c


def _ident(db, contact, kind, value, **kw):
    i = models.ContactIdentity(contact_id=contact.id, user_id=contact.user_id,
                               kind=kind, value=value, **kw)
    db.add(i); db.commit(); db.refresh(i)
    return i


# ── backfill migration ───────────────────────────────────────────────────────

def test_backfill_creates_identities_from_contact_fields(db):
    u = _user(db)
    _contact(db, u, "em:hashed", name="Jane", email="Jane@Foo.com",
             phone="+1 (415) 555-1234", linkedin_public_id="jane-doe")
    dbmod._migrate_contact_identities()

    rows = db.query(models.ContactIdentity).all()
    by_kind = {r.kind: r for r in rows}
    assert set(by_kind) == {"email", "phone", "linkedin"}
    assert by_kind["email"].value == "jane@foo.com"   # normalized lowercase
    assert by_kind["phone"].value == "4155551234"     # last 10 digits
    assert by_kind["linkedin"].value == "jane-doe"
    assert by_kind["email"].is_primary is True         # primary key was em:
    assert by_kind["phone"].is_primary is False
    assert all(r.source == "backfill" and r.confidence == 1.0 for r in rows)


def test_backfill_idempotent(db):
    u = _user(db)
    _contact(db, u, "li:jane-doe", linkedin_public_id="jane-doe", email="j@x.com")
    dbmod._migrate_contact_identities()
    dbmod._migrate_contact_identities()  # run twice
    assert db.query(models.ContactIdentity).count() == 2


# ── duplicate detection ──────────────────────────────────────────────────────

def test_cluster_shared_email(db):
    # Two Contact rows that each carry the SAME email in their own field -- the
    # real shape of a pre-merge duplicate (the unique ContactIdentity constraint
    # prevents two identity rows with the same value, so detection falls back to
    # the contacts' own fields).
    u = _user(db)
    a = _contact(db, u, "li:jane", name="Jane", linkedin_public_id="jane",
                 email="jane@foo.com")
    b = _contact(db, u, "em:h", name="Jane R", email="jane@foo.com")

    clusters = idy.find_duplicate_clusters(db, u)
    assert len(clusters) == 1
    assert clusters[0]["contact_ids"] == sorted([a.id, b.id])
    assert any(s["kind"] == "email" for s in clusters[0]["signals"])


def test_cluster_shared_linkedin(db):
    u = _user(db)
    a = _contact(db, u, "li:jane", linkedin_public_id="jane")
    b = _contact(db, u, "em:h", email="jane@x.com", linkedin_public_id="jane")

    clusters = idy.find_duplicate_clusters(db, u)
    assert len(clusters) == 1
    assert clusters[0]["contact_ids"] == sorted([a.id, b.id])
    assert any(s["kind"] == "linkedin" for s in clusters[0]["signals"])


def test_cluster_bridge_record(db):
    """An email-only contact and a linkedin-only contact, joined by a third
    BRIDGE contact carrying BOTH that email AND that linkedin id."""
    u = _user(db)
    email_only = _contact(db, u, "em:h", email="jane@foo.com")
    li_only = _contact(db, u, "li:jane", linkedin_public_id="jane")
    # The bridge contact carries BOTH identities (e.g. enriched), each matching a
    # different existing contact. Only the bridge holds explicit identity rows;
    # the other two are matched via their own fields (so no unique collision).
    bridge = _contact(db, u, "li:jane2", name="Jane (merged)")
    _ident(db, bridge, "email", "jane@foo.com")
    _ident(db, bridge, "linkedin", "jane")

    clusters = idy.find_duplicate_clusters(db, u)
    assert len(clusters) == 1
    assert clusters[0]["contact_ids"] == sorted(
        [email_only.id, li_only.id, bridge.id])
    assert clusters[0]["bridge"] is True


def test_no_merge_on_first_name_only(db):
    u = _user(db)
    a = _contact(db, u, "em:a", name="Jane", email="jane@a.com")
    b = _contact(db, u, "em:b", name="Jane", email="jane@b.com")
    _ident(db, a, "email", "jane@a.com")
    _ident(db, b, "email", "jane@b.com")

    clusters = idy.find_duplicate_clusters(db, u)
    assert clusters == []


def test_name_plus_company_domain_is_review_only(db):
    u = _user(db)
    a = _contact(db, u, "em:a", name="Jane Doe", email="jane@a.com",
                 company_domain="acme.com")
    b = _contact(db, u, "em:b", name="Jane Doe", email="jdoe@b.com",
                 company_domain="acme.com")
    _ident(db, a, "email", "jane@a.com")
    _ident(db, b, "email", "jdoe@b.com")

    assert idy.find_duplicate_clusters(db, u) == []   # never auto-merged
    review = idy.find_review_candidates(db, u)
    assert len(review) == 1
    assert review[0]["contact_ids"] == sorted([a.id, b.id])
    assert review[0]["auto_merge"] is False


# ── merge ────────────────────────────────────────────────────────────────────

def _seed_children(db, user, contact):
    # (Prospect requires event_id; not needed to prove FK reassignment here.)
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="manual_note",
        interaction_type="note", title="hi"))
    db.add(models.ContactFact(user_id=user.id, contact_id=contact.id,
                              key="interest", value="climbing"))
    db.add(models.OutgoingMessage(user_id=user.id, contact_id=contact.id,
                                  channel="email", body="hello"))
    db.commit()


def test_merge_reassigns_children_and_deletes_duplicate(db):
    u = _user(db)
    survivor = _contact(db, u, "li:jane", name="Jane", linkedin_public_id="jane")
    dup = _contact(db, u, "em:h", email="jane@foo.com")
    _ident(db, survivor, "linkedin", "jane")
    _ident(db, dup, "email", "jane@foo.com")
    _seed_children(db, u, dup)

    plan = idy.merge_contacts(db, survivor_id=survivor.id,
                              duplicate_id=dup.id, apply=True)
    assert plan["applied"] is True
    assert plan["moved"]["interactions"] == 1
    assert plan["moved"]["facts"] == 1
    assert plan["moved"]["outgoing_messages"] == 1
    assert plan["moved"]["identities"] == 1

    # duplicate gone
    assert db.get(models.Contact, dup.id) is None
    # children reassigned
    assert db.query(models.RelationshipInteraction).filter_by(
        contact_id=survivor.id).count() == 1
    assert db.query(models.ContactFact).filter_by(
        contact_id=survivor.id).count() == 1
    assert db.query(models.OutgoingMessage).filter_by(
        contact_id=survivor.id).count() == 1
    # identity moved to survivor + survivor scalar backfilled
    db.refresh(survivor)
    assert survivor.email == "jane@foo.com"
    kinds = {i.kind for i in db.query(models.ContactIdentity).filter_by(
        contact_id=survivor.id).all()}
    assert kinds == {"linkedin", "email"}


def test_merge_dry_run_changes_nothing(db):
    u = _user(db)
    survivor = _contact(db, u, "li:jane", linkedin_public_id="jane")
    dup = _contact(db, u, "em:h", email="jane@foo.com")
    _seed_children(db, u, dup)

    before_dup = db.get(models.Contact, dup.id)
    plan = idy.merge_contacts(db, survivor_id=survivor.id, duplicate_id=dup.id)
    assert plan["applied"] is False
    assert plan["moved"]["interactions"] == 1   # plan computed
    # nothing actually changed
    assert db.get(models.Contact, dup.id) is before_dup
    assert db.query(models.OutgoingMessage).filter_by(
        contact_id=dup.id).count() == 1


def test_merge_idempotent(db):
    u = _user(db)
    survivor = _contact(db, u, "li:jane", linkedin_public_id="jane")
    dup = _contact(db, u, "em:h", email="jane@foo.com")
    idy.merge_contacts(db, survivor_id=survivor.id, duplicate_id=dup.id, apply=True)
    # second call : duplicate already gone -> no-op, no error
    plan2 = idy.merge_contacts(db, survivor_id=survivor.id,
                               duplicate_id=dup.id, apply=True)
    assert plan2["noop"] is True
    assert plan2["applied"] is False


# ── backfill_merge orchestration ─────────────────────────────────────────────

def test_backfill_merge_dry_run_then_apply(db):
    u = _user(db)
    a = _contact(db, u, "li:jane", name="Jane", linkedin_public_id="jane",
                 email="jane@foo.com")
    b = _contact(db, u, "em:h", email="jane@foo.com")
    _seed_children(db, u, b)

    # dry run : nothing changes
    report = idy.backfill_merge(db, u)
    assert report["dry_run"] is True
    assert report["would_merge"] == 1
    assert len(report["clusters"]) == 1
    assert db.query(models.Contact).count() == 2

    # apply : merges
    report2 = idy.backfill_merge(db, u, apply=True)
    assert report2["dry_run"] is False
    assert report2["would_merge"] == 1
    assert db.query(models.Contact).count() == 1
    surviving = db.query(models.Contact).one()
    assert db.query(models.OutgoingMessage).filter_by(
        contact_id=surviving.id).count() == 1
