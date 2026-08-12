"""tests/test_seed_operator.py : referencing a REAL account into an Observe
evaluation cohort.

seed_operator does not generate anything -- it tags rows the sync path
already produced. That makes two things load-bearing, and both were broken:

  1. The tags must not claim the rows were generated. Real synced rows were
     being labelled BASELINE ("generated_from_assumed_distribution"), a false
     statement about origin in the one table whose job is recording origin.

  2. Cleanup must not be deletion. delete_cohort walks the provenance index
     and deletes what it points at, and the module's own docstring
     recommended it -- which deleted a real account's entire book, 40
     contacts and 40 interactions.

Plus the reason the whole thing silently did nothing: the USER row was never
tagged, so cohort_query.users_and_contacts() found no lawyers and every
data-driven harness reported 0/0 while the script printed success.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import provenance as prov, seed_operator
from backend.observe import cohort_query as cq, logstream

UTC = timezone.utc


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


@pytest.fixture
def real_account(db):
    """A genuine account with synced data -- nothing generated, no cohort."""
    u = models.User(email="real@example.com", name="Real Lawyer", is_demo=False,
                    unipile_account_id="acct_1", practice_area="regulatory")
    db.add(u)
    db.flush()
    now = datetime.now(UTC)
    for i in range(12):
        c = models.Contact(user_id=u.id, primary_identity_key=f"li:{i}",
                           name=f"Real Person {i}")
        db.add(c)
        db.flush()
        db.add(models.RelationshipInteraction(
            actor_user_id=u.id, contact_id=c.id, occurred_at=now - timedelta(days=i + 1),
            source_type="linkedin_dm", interaction_type="message",
            title="LinkedIn message"))
        db.add(models.RelationshipInteraction(
            actor_user_id=u.id, contact_id=c.id, occurred_at=now - timedelta(days=i + 2),
            source_type="draft", interaction_type="message",
            title="Drafted follow-up (stale_reengage)",
            meta_json=json.dumps({"signal_category": "job_change",
                                  "disposition": "approved_as_is"})))
    db.commit()
    return u


def test_real_rows_are_never_labelled_as_generated(db, real_account):
    seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")
    tags = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == "obs-1")).scalars().all()
    assert tags
    for t in tags:
        assert t.data_provenance == prov.OBSERVED
        assert t.data_provenance not in prov.GENERATED_VALUES, \
            "a real synced row is tagged as something this package generated"


def test_delete_cohort_untags_observed_rows_instead_of_deleting_them(db, real_account):
    """The bug that destroyed a real book. delete_cohort is the documented
    cleanup path; on an OBSERVED cohort it must remove tags and nothing else."""
    seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")
    before_contacts = db.query(models.Contact).count()
    before_interactions = db.query(models.RelationshipInteraction).count()
    before_users = db.query(models.User).count()
    assert db.query(models.DemoProvenance).count() > 0

    deleted = prov.delete_cohort(db, "obs-1")

    assert deleted == 0, "delete_cohort destroyed real account data"
    assert db.query(models.Contact).count() == before_contacts
    assert db.query(models.RelationshipInteraction).count() == before_interactions
    assert db.query(models.User).count() == before_users
    assert db.query(models.DemoProvenance).count() == 0, "tags were not removed"


def test_delete_cohort_still_deletes_generated_rows(db):
    """The guard must not turn cleanup into a no-op for real cohorts."""
    from backend.demo import cohort

    cohort_id = cohort.generate(db, n_lawyers=2, days=14, cohort_id="gen-1")
    assert db.query(models.Contact).count() > 0
    deleted = prov.delete_cohort(db, cohort_id)
    assert deleted > 0
    assert db.query(models.Contact).count() == 0


def test_the_user_row_is_tagged_so_harnesses_can_find_the_lawyer(db, real_account):
    """Without a users tag, users_and_contacts() returns nothing and every
    data-driven harness reports 0/0 -- while the script prints success."""
    seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")

    pairs = cq.users_and_contacts(db, "obs-1")
    assert pairs, "no lawyer resolved for the cohort -- harnesses would report 0/0"
    user, contacts = pairs[0]
    assert user.email == "real@example.com"
    assert len(contacts) == 12


def test_an_observed_cohort_becomes_the_evaluation_dataset(db, real_account):
    """Matching BASELINE alone would silently ignore it."""
    seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")
    assert logstream.evaluation_cohort_id(db) == "obs-1"


def test_synthetic_fixtures_still_never_become_the_evaluation_dataset(db, real_account):
    """Widening the predicate must not readmit the known-answer fixtures."""
    from backend.observe.harnesses import synthetic_scenarios

    seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")
    synthetic_scenarios.run(db)      # writes strictly newer rows

    newest = db.execute(select(models.DemoProvenance.cohort_id)
                        .order_by(models.DemoProvenance.id.desc()).limit(1)).scalar_one()
    assert newest == synthetic_scenarios.COHORT_ID          # the trap
    assert logstream.evaluation_cohort_id(db) == "obs-1"


def test_seeding_is_scoped_to_the_named_account(db, real_account):
    """Tagging every Unipile-connected user mixes several people's books into
    one evaluation set -- a privacy surprise, and meaningless to evaluate."""
    other = models.User(email="someone-else@example.com", name="Other",
                        is_demo=False, unipile_account_id="acct_2")
    db.add(other)
    db.flush()
    db.add(models.Contact(user_id=other.id, primary_identity_key="li:x", name="Theirs"))
    db.commit()

    stats = seed_operator.seed_from_real_account(db, "real@example.com", "obs-1")
    assert stats["contacts"] == 12

    tagged_contact_ids = {t.row_id for t in db.execute(
        select(models.DemoProvenance).where(
            models.DemoProvenance.cohort_id == "obs-1",
            models.DemoProvenance.table_name == "contacts")).scalars().all()}
    theirs = db.execute(select(models.Contact).where(
        models.Contact.user_id == other.id)).scalars().first()
    assert theirs.id not in tagged_contact_ids, "another account's contact was pulled in"


def test_evaluability_is_reported_honestly(db):
    """A cohort can be large and still evaluate nothing: the harnesses grade
    against drafted follow-ups. Saying so beats a mysterious 0/0."""
    u = models.User(email="nodrafts@example.com", name="No Drafts", is_demo=False,
                    unipile_account_id="acct_3")
    db.add(u)
    db.flush()
    for i in range(5):
        c = models.Contact(user_id=u.id, primary_identity_key=f"nd:{i}", name=f"P{i}")
        db.add(c)
        db.flush()
        db.add(models.RelationshipInteraction(
            actor_user_id=u.id, contact_id=c.id, occurred_at=datetime.now(UTC),
            source_type="linkedin_dm", interaction_type="message", title="msg"))
    db.commit()

    stats = seed_operator.seed_from_real_account(db, "nodrafts@example.com", "obs-2")
    assert stats["contacts"] == 5
    assert stats["evaluable_contacts"] == 0, \
        "reported evaluable contacts where no drafted follow-up exists"


def test_missing_account_is_reported_not_silently_empty(db):
    stats = seed_operator.seed_from_real_account(db, "nobody@example.com", "obs-3")
    assert stats == {"users": 0, "contacts": 0, "interactions": 0, "evaluable_contacts": 0}
    assert db.query(models.DemoProvenance).count() == 0


# ── the broad --all-unipile-users form (kept from #324, which fixed the
#    missing users tag in parallel) ─────────────────────────────────────────

def _unipile_user(db, email="lawyer@firm.com"):
    u = models.User(email=email, name="Lawyer", password_hash="h",
                    unipile_account_id="acct_1")
    db.add(u)
    db.flush()
    c = models.Contact(user_id=u.id, primary_identity_key="li:1", name="Client A")
    db.add(c)
    db.flush()
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="linkedin",
        interaction_type="message", direction="outbound", title="Re: intro",
        summary="real synced message"))
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
    """The downstream consumer every cohort-based harness shares -- it
    resolves lawyers strictly off the users tag."""
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


def test_the_broad_form_is_also_safe_to_clean_up(db):
    """--all-unipile-users tags real rows too, so delete_cohort must be
    non-destructive there for exactly the same reason."""
    _unipile_user(db)
    seed_operator.seed_from_unipile_users(db, "test-cohort")
    before = db.query(models.Contact).count()

    assert prov.delete_cohort(db, "test-cohort") == 0
    assert db.query(models.Contact).count() == before
    assert db.query(models.DemoProvenance).count() == 0
