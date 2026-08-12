"""tests/test_seed_operator.py : backend/demo/seed_operator.py --unipile-users.

seed_from_unipile_users() tags real Contact/RelationshipInteraction rows with
DemoProvenance but, before this fix, never tagged the User row itself --
cohort_query.users_and_contacts() (shared by every cohort-based harness)
resolves a cohort's lawyers strictly from a table_name=="users" tag, so every
harness silently found zero lawyers for a Unipile-seeded cohort no matter how
much contact/interaction data was tagged underneath.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import seed_operator
from backend.observe import cohort_query as cq


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _unipile_user(db, email="lawyer@firm.com"):
    u = models.User(email=email, name="Lawyer", password_hash="h",
                    unipile_account_id="acct_1")
    db.add(u)
    db.flush()
    c = models.Contact(user_id=u.id, primary_identity_key="li:1", name="Client A")
    db.add(c)
    db.flush()
    ri = models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="linkedin",
        interaction_type="message", direction="outbound", title="Re: intro",
        summary="real synced message",
    )
    db.add(ri)
    db.commit()
    return u


def test_seed_from_unipile_users_tags_the_user_row(db):
    user = _unipile_user(db)
    stats = seed_operator.seed_from_unipile_users(db, "test-cohort")
    assert stats["users"] == 1

    tag = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == "test-cohort",
        models.DemoProvenance.table_name == "users")).scalar_one_or_none()
    assert tag is not None
    assert tag.row_id == user.id


def test_cohort_query_resolves_a_unipile_seeded_cohort(db):
    """The actual downstream consumer every cohort-based harness shares --
    before the fix, this returned [] even though contacts/interactions were
    tagged, because it resolves lawyers strictly off the users tag."""
    user = _unipile_user(db)
    seed_operator.seed_from_unipile_users(db, "test-cohort")

    pairs = cq.users_and_contacts(db, "test-cohort")
    assert len(pairs) == 1
    resolved_user, contacts = pairs[0]
    assert resolved_user.id == user.id
    assert len(contacts) == 1


def test_seed_from_unipile_users_skips_non_unipile_accounts(db):
    plain = models.User(email="no-unipile@example.com", name="Plain", password_hash="h")
    db.add(plain)
    db.commit()

    stats = seed_operator.seed_from_unipile_users(db, "test-cohort")
    assert stats["users"] == 0
    assert cq.users_and_contacts(db, "test-cohort") == []
