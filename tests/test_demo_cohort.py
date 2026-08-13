"""Tests for backend/demo -- the 80-lawyer/30-day demo cohort generator.

Verifies: every generated row is provenance-tagged, the baseline layer is
schema-faithful (real tables, real taxonomy), the extended layer actually
uses the real solicitation gate (not a fabricated allow/block split), and
delete_cohort leaves nothing behind.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort, extended, provenance as prov


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


def test_generate_writes_real_rows_with_baseline_provenance(db):
    cohort_id = cohort.generate(db, n_lawyers=5, days=14)
    counts = prov.cohort_row_counts(db, cohort_id)
    assert counts["users"] == {prov.BASELINE: 5}
    assert counts["contacts"][prov.BASELINE] > 0
    assert counts["relationship_interactions"][prov.BASELINE] > 0

    # Real production rows, not a parallel schema.
    users = db.execute(
        select(models.User).where(models.User.email.like("demo-lawyer-%"))
    ).scalars().all()
    assert len(users) == 5
    assert all(u.is_demo for u in users)  # reuses the existing demo flag, no new concept


def test_every_generated_row_has_a_provenance_tag(db):
    cohort.generate(db, n_lawyers=3, days=7)
    all_tags = db.execute(select(models.DemoProvenance)).scalars().all()
    all_users = db.execute(select(models.User)).scalars().all()
    all_contacts = db.execute(select(models.Contact)).scalars().all()
    tagged_ids = {(t.table_name, t.row_id) for t in all_tags}
    for u in all_users:
        assert ("users", u.id) in tagged_ids
    for c in all_contacts:
        assert ("contacts", c.id) in tagged_ids


def test_deterministic_for_same_cohort_id(db):
    cohort.generate(db, n_lawyers=3, days=7, cohort_id="fixed-test-cohort")
    users_a = sorted(u.name for u in db.execute(select(models.User)).scalars().all())

    db.query(models.User).delete()
    db.query(models.Contact).delete()
    db.query(models.RelationshipInteraction).delete()
    db.query(models.DemoProvenance).delete()
    db.commit()

    cohort.generate(db, n_lawyers=3, days=7, cohort_id="fixed-test-cohort")
    users_b = sorted(u.name for u in db.execute(select(models.User)).scalars().all())
    assert users_a == users_b


def test_delete_cohort_removes_everything(db):
    cohort_id = cohort.generate(db, n_lawyers=4, days=14)
    before = prov.cohort_row_counts(db, cohort_id)
    assert sum(sum(v.values()) for v in before.values()) > 0

    deleted = prov.delete_cohort(db, cohort_id)
    assert deleted == sum(sum(v.values()) for v in before.values())
    after = prov.cohort_row_counts(db, cohort_id)
    assert after == {}
    assert db.execute(select(models.User)).scalars().all() == []


def test_extended_jurisdiction_layer_uses_the_real_gate(db):
    # Pinned cohort_id, because generate() otherwise derives one from the
    # wall-clock SECOND (f"baseline-{now:%Y%m%d%H%M%S}") and seeds every rng
    # off it. The assertion below needs the draw across 10 lawyers to contain
    # both an allow and a block, which most seconds satisfy and some do not --
    # so this failed intermittently in full-suite runs and passed every time
    # it was re-run in isolation. A fixed id makes the draw reproducible.
    cohort_id = cohort.generate(db, n_lawyers=10, days=30,
                                cohort_id="jurisdiction-gate-fixture")
    result = extended.run_all(db, cohort_id)

    j_counts = prov.cohort_row_counts(db, result["jurisdiction_cohort_id"])
    assert j_counts  # wrote something
    assert prov.EXTENDED in next(iter(j_counts.values()))

    tags = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == result["jurisdiction_cohort_id"])).scalars().all()
    rows = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.id.in_([t.row_id for t in tags]))).scalars().all()
    titles = {r.title for r in rows}
    # Must show real discrimination, not an always-allow / always-block gate.
    assert "Jurisdiction gate: allowed" in titles
    assert "Jurisdiction gate: blocked" in titles


def test_extended_ranking_uses_the_real_health_heuristic(db):
    cohort_id = cohort.generate(db, n_lawyers=10, days=30)
    ranking = extended.compute_match_scores(db, cohort_id)
    assert ranking
    assert all(r["status"] in ("active", "warm", "cooling", "dormant") for r in ranking)
    # Sorted descending by opportunity_score.
    scores = [r["opportunity_score"] for r in ranking]
    assert scores == sorted(scores, reverse=True)


def test_baseline_and_extended_provenance_are_distinguishable(db):
    cohort_id = cohort.generate(db, n_lawyers=5, days=14)
    extended.run_all(db, cohort_id)
    all_tags = db.execute(select(models.DemoProvenance)).scalars().all()
    provenances = {t.data_provenance for t in all_tags}
    assert prov.BASELINE in provenances
    assert prov.EXTENDED in provenances
    assert provenances <= prov.PROVENANCE_VALUES
