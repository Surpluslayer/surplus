"""tests/test_ranking_prefetch_equivalence.py : the ranking prefetch is a
cache, and a cache is only allowed to change SPEED.

ranking_trace.prefetch() exists because scoring N candidates issued 2N
queries and _historical_behavior re-scanned every draft on the account once
per candidate -- ~1,560 queries for a single contact click, which measured
~150s against a networked Postgres.

The risk in that kind of change is that the numbers quietly drift. These
tests compare the prefetched path against the original per-contact-query
path field by field: same factor values, same evidence dicts, same scores,
same order. Timing tests would pass just as well on wrong answers, so the
equivalence check is the one that matters.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort, ranking_trace as rt
from backend.observe.harnesses import ablation

UTC = timezone.utc


@pytest.fixture
def book():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    cohort.generate(db, n_lawyers=1, days=30, cohort_id="prefetch-eq",
                    contacts_per_lawyer=(25, 25))
    db.commit()
    user = db.execute(select(models.User).where(
        models.User.email == "demo-lawyer-000@example.com")).scalar_one()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()

    # Real books carry draft history with engagement outcomes; the generated
    # cohort is sparse, and historical_behavior is exactly the factor the
    # prefetch changes most. Give it something to aggregate.
    now = datetime.now(UTC)
    cats = ["litigation_filed", "job_change", "funding_round"]
    rows = []
    for i, c in enumerate(contacts):
        cat = cats[i % len(cats)]
        rows.append(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=c.id,
            occurred_at=now - timedelta(days=i + 1),
            source_type="activity_update", interaction_type="post",
            title="New LinkedIn post",
            meta_json=json.dumps({"signal_category": cat, "signal_kind": "new_post"})))
        rows.append(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=c.id,
            occurred_at=now - timedelta(days=i + 2),
            source_type="draft", interaction_type="message",
            title="Drafted follow-up (stale_reengage)",
            meta_json=json.dumps({"signal_category": cat, "engaged": i % 3 == 0})))
        rows.append(models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=c.id,
            occurred_at=now - timedelta(days=i + 5),
            source_type="message", interaction_type="message", title="Message"))
    db.add_all(rows)
    db.commit()
    try:
        yield db, user, contacts
    finally:
        db.close()


def _plain(db, user, contacts, **kw):
    """The original path: every factor does its own per-contact query."""
    traces = [rt.compute_trace(db, user, c, data=None, **kw) for c in contacts]
    traces.sort(key=lambda t: t.opportunity_score, reverse=True)
    for i, t in enumerate(traces, start=1):
        t.rank = i
    return traces


def _as_comparable(traces):
    return [(t.contact_id, t.rank, round(t.opportunity_score, 12),
             [(f.name, round(f.value, 12), f.evidence) for f in t.factors])
            for t in traces]


def test_prefetched_ranking_matches_the_unprefetched_one_exactly(book):
    db, user, contacts = book
    assert _as_comparable(rt.rank_opportunities(db, user, contacts)) == \
           _as_comparable(_plain(db, user, contacts))


def test_equivalence_holds_for_every_ablation_factor_subset(book):
    """Each lever ranks on a different factor subset; the cache must be
    correct for all of them, not just the full model."""
    db, user, contacts = book
    for group, names in rt.FACTOR_GROUPS.items():
        removed = set(names)
        subset = [n for n in rt.FACTOR_WEIGHTS if n not in removed]
        got = rt.rank_opportunities(db, user, contacts, include_factors=subset)
        want = _plain(db, user, contacts, include_factors=subset)
        assert _as_comparable(got) == _as_comparable(want), f"drift without {group}"


def test_equivalence_holds_under_as_of_point_in_time(book):
    """as_of is baked in at prefetch time. If that filtering diverged, the
    replay harness's no-future-leakage guarantee would be silently wrong."""
    db, user, contacts = book
    for days_back in (1, 7, 30, 90):
        as_of = datetime.now(UTC) - timedelta(days=days_back)
        got = rt.rank_opportunities(db, user, contacts, as_of=as_of)
        want = _plain(db, user, contacts, as_of=as_of)
        assert _as_comparable(got) == _as_comparable(want), f"drift at as_of -{days_back}d"


def test_prefetch_excludes_rows_after_as_of(book):
    """Directly: nothing loaded may postdate the cutoff, for either the
    per-contact rows or the account-wide draft history."""
    db, user, contacts = book
    as_of = datetime.now(UTC) - timedelta(days=10)
    data = rt.prefetch(db, user, contacts, as_of=as_of)
    for rows in data.interactions.values():
        for r in rows:
            assert rt._aware(r.occurred_at) <= as_of
    for sig in data.latest_signal.values():
        if sig is not None:
            assert rt._aware(sig.occurred_at) <= as_of

    later = rt.prefetch(db, user, contacts, as_of=None)
    assert sum(s for s, _sv, _d in later.behavior.values()) >= \
           sum(s for s, _sv, _d in data.behavior.values()), \
           "as_of must not INCREASE the draft history it aggregates"


def test_ablate_one_result_is_unchanged_by_sharing_prefetched_data(book):
    """ablate_one accepts shared data so a 4-lever loop loads once. Passing
    it must not change the answer."""
    db, user, contacts = book
    subject = contacts[0]
    shared = rt.prefetch(db, user, contacts)
    for group in rt.FACTOR_GROUPS:
        assert ablation.ablate_one(db, user, subject, contacts, group, data=shared) == \
               ablation.ablate_one(db, user, subject, contacts, group)


def test_ranking_no_longer_scales_its_query_count_with_the_book(book):
    """The actual regression guard: the number of interaction reads must stay
    flat as the candidate set grows, instead of 2 per contact.

    Counted at the driver, and scoped to relationship_interactions -- the
    reads ranking itself performs. A raw total would also count SQLAlchemy
    re-loading each Contact row, which is expire_on_commit refreshing
    instances the fixture expired by committing, not work ranking asked for.
    Touching the contacts first materializes them so the measurement isolates
    the thing this change controls.
    """
    db, user, contacts = book
    for c in contacts:          # warm the identity map, see docstring
        _ = c.name
    engine = db.get_bind()
    seen: list = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    def interaction_reads(fn):
        seen.clear()
        fn()
        return sum(1 for s in seen if "relationship_interactions" in s)

    try:
        small = interaction_reads(lambda: rt.rank_opportunities(db, user, contacts[:5]))
        full = interaction_reads(lambda: rt.rank_opportunities(db, user, contacts))
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert full == small, (
        f"interaction reads grew with the candidate set ({small} -> {full} for "
        f"5 -> {len(contacts)} contacts); the prefetch is not being used")
    assert full <= 4, f"expected a small constant number of reads, got {full}"


def test_historical_behavior_aggregate_matches_the_scanning_version(book):
    """The account-wide factor: same seen/saved/dismissed either way."""
    db, user, contacts = book
    data = rt.prefetch(db, user, contacts)
    for c in contacts:
        signal = data.latest_signal.get(c.id)
        if signal is None:
            continue
        cached = rt._historical_behavior(db, user, signal, behavior=data.behavior)
        scanned = rt._historical_behavior(db, user, signal)
        assert cached.value == scanned.value
        assert cached.evidence == scanned.evidence


def test_candidate_resolution_picks_the_same_contacts_as_the_per_contact_scan(book):
    """resolve_candidates decides WHAT gets ranked. It used to call
    _latest_signal once per contact in the book; it now reads signal-carrying
    contact ids in bulk. Same partition, or the whole ranking is about a
    different set of people."""
    from backend.observe import pipeline

    db, user, contacts = book
    # Force the bounded path: MAX_RANKING_CANDIDATES must be exceeded for the
    # signal-carrying preference to kick in at all.
    original = pipeline.MAX_RANKING_CANDIDATES
    pipeline.MAX_RANKING_CANDIDATES = 5
    try:
        got, total = pipeline.resolve_candidates(db, user, contacts[0], list(contacts))

        signaled = [c for c in contacts if rt._latest_signal(db, c.id) is not None]
        plain = [c for c in contacts if rt._latest_signal(db, c.id) is None]
        want = (signaled + plain)[:5]
        if contacts[0] not in want:
            want = want + [contacts[0]]

        assert total == len(contacts)
        assert [c.id for c in got] == [c.id for c in want]
    finally:
        pipeline.MAX_RANKING_CANDIDATES = original


def test_candidate_resolution_does_not_query_per_contact(book):
    """The regression guard for the same N+1, on the selection step."""
    from backend.observe import pipeline

    db, user, contacts = book
    for c in contacts:
        _ = c.name
    engine = db.get_bind()
    seen: list = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    original = pipeline.MAX_RANKING_CANDIDATES
    pipeline.MAX_RANKING_CANDIDATES = 5
    try:
        seen.clear()
        pipeline.resolve_candidates(db, user, contacts[0], list(contacts))
        reads = sum(1 for s in seen if "relationship_interactions" in s)
    finally:
        pipeline.MAX_RANKING_CANDIDATES = original
        event.remove(engine, "before_cursor_execute", _count)

    assert reads <= 2, (
        f"{reads} interaction reads to partition {len(contacts)} contacts; "
        "expected a bulk read, not one per contact")
