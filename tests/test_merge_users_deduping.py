"""
Tests for /admin/merge-users-deduping : merging two FULL duplicate accounts for
the same person, deduping contacts by identity key (vs the orphan-only
merge-users). Validates overlap folding, unique reassign, child-row healing
(facts must NOT cascade-die with the source), and dry-run rollback.

Direct-function tests on an in-memory session (same pattern as test_merge_users).
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes.admin import MergeUsersBody, merge_users_deduping


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def _fact(db, user_id, contact_id, key, value):
    now = datetime.now(timezone.utc)
    db.add(models.ContactFact(
        user_id=user_id, contact_id=contact_id, key=key, value=value,
        source="test", confidence=90, observed_at=now, recurring=False,
        dedup_key=f"{key}:{value}", created_at=now, updated_at=now))


def _seed(db):
    """Survivor (dst) and source (src), same person, with:
      - a SHARED contact (same primary_identity_key) on both -> overlap/merge
      - a UNIQUE contact only on src -> reassign
      - facts on the src contacts that must survive the merge."""
    dst = models.User(unipile_account_id="dst_acct", name="Jia",
                      linkedin_public_id="jiahui-jin")
    src = models.User(unipile_account_id="src_acct", name="Jia (dup)",
                      linkedin_public_id="jiahui-jin",
                      email="jia@example.com",
                      paid_at=datetime.now(timezone.utc),
                      stripe_customer_id="cus_paid")
    db.add_all([dst, src]); db.flush()

    # shared person on both accounts
    dst_shared = models.Contact(user_id=dst.id, primary_identity_key="li:alice",
                                name="Alice")
    src_shared = models.Contact(user_id=src.id, primary_identity_key="li:alice",
                                name="Alice")
    # person only on src
    src_only = models.Contact(user_id=src.id, primary_identity_key="li:bob",
                              name="Bob")
    db.add_all([dst_shared, src_shared, src_only]); db.flush()

    _fact(db, src.id, src_shared.id, "role", "CTO")     # must move to dst_shared
    _fact(db, src.id, src_only.id, "city", "NYC")       # must ride along with Bob
    db.commit()
    return src, dst, dst_shared.id


def test_dry_run_changes_nothing(db):
    src, dst, _ = _seed(db)
    res = merge_users_deduping(MergeUsersBody(
        from_user_id=src.id, to_user_id=dst.id, dry_run=True), db=db, _=None)
    assert res["dry_run"] is True
    assert res["contacts_merged"] == 1      # Alice
    assert res["contacts_reassigned"] == 1  # Bob
    # nothing persisted
    assert db.get(models.User, src.id) is not None
    assert db.query(models.Contact).filter_by(user_id=src.id).count() == 2


def test_apply_dedups_and_preserves_facts(db):
    src, dst, dst_shared_id = _seed(db)
    res = merge_users_deduping(MergeUsersBody(
        from_user_id=src.id, to_user_id=dst.id, dry_run=False), db=db, _=None)
    assert res["dry_run"] is False

    # source is gone, and every contact now belongs to the survivor.
    assert db.get(models.User, src.id) is None
    assert db.query(models.Contact).filter_by(user_id=src.id).count() == 0
    # survivor has exactly the 2 UNIQUE people (Alice merged, Bob moved), no dup.
    surv_contacts = db.query(models.Contact).filter_by(user_id=dst.id).all()
    keys = sorted(c.primary_identity_key for c in surv_contacts)
    assert keys == ["li:alice", "li:bob"]

    # Alice's src fact (role=CTO) survived onto the survivor's Alice contact.
    facts = db.query(models.ContactFact).all()
    # every fact is now owned by the survivor (none orphaned to the deleted src).
    assert all(f.user_id == dst.id for f in facts)
    alice_facts = {f.key for f in facts if f.contact_id == dst_shared_id}
    assert "role" in alice_facts
    # Bob's fact rode along, still present.
    assert any(f.key == "city" for f in facts)

    # billing + email + keys gap-filled onto survivor.
    survivor = db.get(models.User, dst.id)
    assert survivor.paid_at is not None
    assert survivor.stripe_customer_id == "cus_paid"
    assert survivor.email == "jia@example.com"


def test_no_facts_cascade_deleted(db):
    """The whole point: after merge, no fact is lost to the src cascade."""
    src, dst, _ = _seed(db)
    before = db.query(models.ContactFact).count()
    merge_users_deduping(MergeUsersBody(
        from_user_id=src.id, to_user_id=dst.id, dry_run=False), db=db, _=None)
    after = db.query(models.ContactFact).count()
    assert after == before  # 2 facts in, 2 facts out
