"""tests/test_observe_candidates_and_levers.py : the full-candidate-ranking-
view + beat-the-alternative comparison (adapters.opportunity_trace), and the
two new granular ablation levers (signal_affinity, timing) plus the
relationship_context evidence added to relationship_trace.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort, ranking_trace as rt
from backend.observe import adapters
from backend.observe.harnesses import ablation


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
def user_and_contacts(db):
    cohort.generate(db, n_lawyers=10, days=30)
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    return user, contacts


# ── candidate list + comparison ─────────────────────────────────────────

def test_opportunity_trace_lists_every_candidate_with_score_and_rank(db, user_and_contacts):
    user, contacts = user_and_contacts
    trace = adapters.opportunity_trace(db, user, contacts[0], contacts)
    d = trace.to_dict()
    listed = d["evidence"]["candidates_considered"]
    assert d["evidence"]["candidates_considered_total"] == len(contacts)
    assert len(listed) <= adapters.MAX_CANDIDATES_IN_EVIDENCE
    for row in listed:
        assert "rank" in row and "score" in row and "contact_id" in row
    ranks = [row["rank"] for row in listed]
    assert ranks == sorted(ranks)  # ordered by rank


def test_opportunity_trace_comparison_matches_ranked_reality(db, user_and_contacts):
    user, contacts = user_and_contacts
    ranked = rt.rank_opportunities(db, user, contacts)
    top = ranked[0]
    top_contact = next(c for c in contacts if c.id == top.contact_id)

    trace = adapters.opportunity_trace(db, user, top_contact, contacts)
    comparison = trace.evidence["comparison"]
    assert comparison is not None
    assert comparison["this_beat_alternative"] is True
    assert comparison["alternative_rank"] == 2
    runner_up = next(t for t in ranked if t.rank == 2)
    assert comparison["alternative_contact_id"] == runner_up.contact_id
    assert abs(comparison["score_delta"] - (top.opportunity_score - runner_up.opportunity_score)) < 1e-3
    assert comparison["largest_contributing_factor"] is not None


def test_opportunity_trace_comparison_for_a_losing_candidate_shows_who_it_lost_to(db, user_and_contacts):
    user, contacts = user_and_contacts
    ranked = rt.rank_opportunities(db, user, contacts)
    if len(ranked) < 2:
        pytest.skip("not enough candidates")
    second = ranked[1]
    second_contact = next(c for c in contacts if c.id == second.contact_id)
    trace = adapters.opportunity_trace(db, user, second_contact, contacts)
    comparison = trace.evidence["comparison"]
    assert comparison["this_beat_alternative"] is False
    assert comparison["alternative_rank"] == 1


def test_single_candidate_has_no_comparison(db, user_and_contacts):
    user, contacts = user_and_contacts
    trace = adapters.opportunity_trace(db, user, contacts[0], [contacts[0]])
    assert trace.evidence["comparison"] is None


# ── granular ablation levers ─────────────────────────────────────────────

def test_signal_affinity_and_timing_are_registered_factor_groups():
    assert "signal_affinity" in rt.FACTOR_GROUPS
    assert "timing" in rt.FACTOR_GROUPS
    assert rt.FACTOR_GROUPS["signal_affinity"] == ("practice_fit",)
    assert rt.FACTOR_GROUPS["timing"] == ("relationship_recency", "relationship_trajectory")


def test_ablate_one_all_four_levers_never_double_counts_a_factor(db, user_and_contacts):
    """The regression this test guards: FACTOR_GROUPS has overlapping
    entries (signal_affinity subsets signal_practice; timing subsets
    relationship). ablate_one's "full system" score must be identical
    across all four levers -- if a factor were double-counted, it wouldn't
    be."""
    user, contacts = user_and_contacts
    full_scores = set()
    for group in ("relationship", "behavior", "signal_affinity", "timing"):
        result = ablation.ablate_one(db, user, contacts[0], contacts, remove_group=group)
        full_scores.add(result["full_system"]["score"])
    assert len(full_scores) == 1, f"full_system score differs by lever: {full_scores}"


def test_ablate_one_timing_is_a_strict_subset_of_relationship(db, user_and_contacts):
    """timing (recency+trajectory) removes strictly fewer factors than
    relationship (strength+recency+trajectory) -- when the removed set
    differs, the two "without" results are independently valid ablations,
    not required to move the score in the same direction (renormalization
    means removing a below-average factor can raise the score), but they
    must be genuinely different runs, not the same computation twice."""
    user, contacts = user_and_contacts
    rel = ablation.ablate_one(db, user, contacts[0], contacts, remove_group="relationship")
    timing = ablation.ablate_one(db, user, contacts[0], contacts, remove_group="timing")
    assert rel["full_system"]["score"] == timing["full_system"]["score"]
    assert set(rt.FACTOR_GROUPS["timing"]) < set(rt.FACTOR_GROUPS["relationship"])


# ── relationship_context evidence ────────────────────────────────────────

def test_relationship_trace_includes_duration_and_shared_entities(db, user_and_contacts):
    user, contacts = user_and_contacts
    trace = adapters.relationship_trace(db, user, contacts[0])
    ctx = trace.evidence
    assert "relationship_duration_days" in ctx
    assert "shared_entities" in ctx
    assert isinstance(ctx["shared_entities"], int)
    assert ctx["topic_overlap"] is None  # honestly not computed, not fabricated
    assert ctx["practice_overlap"] is None


def test_shared_entities_counts_same_company_contacts(db):
    user = models.User(email="shared@example.com", name="Shared Test", is_demo=True)
    db.add(user)
    db.flush()
    c1 = models.Contact(user_id=user.id, primary_identity_key="s:1", name="A", company="Acme Co")
    c2 = models.Contact(user_id=user.id, primary_identity_key="s:2", name="B", company="Acme Co")
    c3 = models.Contact(user_id=user.id, primary_identity_key="s:3", name="C", company="Other Co")
    db.add_all([c1, c2, c3])
    db.commit()

    trace = adapters.relationship_trace(db, user, c1)
    assert trace.evidence["shared_entities"] == 1  # c2, not c3

    trace3 = adapters.relationship_trace(db, user, c3)
    assert trace3.evidence["shared_entities"] == 0
