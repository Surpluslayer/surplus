"""tests/test_affinity_learning.py : the outcome → taxonomy loop, end to end.

`update_affinity_from_outcomes()` was implemented and tested and never
called. Worse, it could not have worked if it had been: NOTHING in production
ever wrote a draft disposition, so the only outcomes in the database came
from the demo cohort generator. "The taxonomy learns from outcomes" was true
of generated data and false of real usage.

Closing it needed three things, and these tests cover all three:

  1. real outcomes get recorded when a lawyer actually acts,
  2. the aggregate is derived from those and persisted, and
  3. affinity() consults it -- shrunk toward the seed, so n=1 cannot swing a
     prior to 0.0 or 1.0 and call that a measurement.
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
from backend.agents.relationship import outcomes
from backend.demo import provenance as prov, signal_taxonomy as tax

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


def _lawyer(db, practice_area="corporate_ma", email="lawyer@firm.com"):
    u = models.User(email=email, name="Lawyer", is_demo=False,
                    practice_area=practice_area)
    db.add(u)
    db.flush()
    return u


def _drafted(db, user, contact, category="litigation_filed", draft="Hi there, congrats."):
    row = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        occurred_at=datetime.now(UTC) - timedelta(hours=1),
        source_type="draft", interaction_type="message",
        title="Drafted follow-up (stale_reengage)",
        meta_json=json.dumps({"signal_category": category, "draft": draft}))
    db.add(row)
    db.flush()
    return row


def _contact(db, user, key="li:1"):
    c = models.Contact(user_id=user.id, primary_identity_key=key, name="Client")
    db.add(c)
    db.flush()
    return c


# ── 1. recording a real outcome ─────────────────────────────────────────────

def test_sending_the_draft_unchanged_records_approved_as_is(db):
    u = _lawyer(db)
    c = _contact(db, u)
    _drafted(db, u, c, draft="Hi there, congrats.")
    db.commit()

    row = outcomes.record_draft_outcome(db, u.id, c.id, sent_body="Hi there, congrats.")
    meta = json.loads(row.meta_json)
    assert meta["disposition"] == "approved_as_is"
    assert meta["engaged"] is True


def test_sending_an_edited_draft_records_edited_then_sent(db):
    u = _lawyer(db)
    c = _contact(db, u)
    _drafted(db, u, c, draft="Hi there, congrats.")
    db.commit()

    row = outcomes.record_draft_outcome(db, u.id, c.id,
                                        sent_body="Hi Dana -- congratulations, genuinely.")
    assert json.loads(row.meta_json)["disposition"] == "edited_then_sent"


def test_whitespace_and_case_do_not_count_as_an_edit(db):
    """A trailing newline is not the lawyer rewriting the message."""
    u = _lawyer(db)
    c = _contact(db, u)
    _drafted(db, u, c, draft="Hi there, congrats.")
    db.commit()

    row = outcomes.record_draft_outcome(db, u.id, c.id,
                                        sent_body="  Hi There,   congrats.\n")
    assert json.loads(row.meta_json)["disposition"] == "approved_as_is"


def test_snoozing_records_a_discard(db):
    u = _lawyer(db)
    c = _contact(db, u)
    _drafted(db, u, c)
    db.commit()

    row = outcomes.record_draft_outcome(db, u.id, c.id, disposition="discarded")
    meta = json.loads(row.meta_json)
    assert meta["disposition"] == "discarded"
    assert meta["engaged"] is False


def test_recording_is_idempotent(db):
    """A retried request or a double-click must not inflate the counts the
    taxonomy learns from."""
    u = _lawyer(db)
    c = _contact(db, u)
    _drafted(db, u, c)
    db.commit()

    first = outcomes.record_draft_outcome(db, u.id, c.id, disposition="discarded")
    again = outcomes.record_draft_outcome(db, u.id, c.id, sent_body="anything")
    assert again is None, "an already-decided draft was overwritten"
    assert json.loads(first.meta_json)["disposition"] == "discarded"


def test_an_action_with_no_draft_behind_it_records_nothing(db):
    """A lawyer can send a message no draft prompted. Inventing a link to an
    unrelated draft would corrupt the signal this exists to collect."""
    u = _lawyer(db)
    c = _contact(db, u)
    db.commit()
    assert outcomes.record_draft_outcome(db, u.id, c.id, sent_body="hello") is None


def test_recording_never_raises_into_the_send_path(db):
    """A bookkeeping failure must not turn a successful send into an error."""
    u = _lawyer(db)
    c = _contact(db, u)
    db.commit()
    outcomes.record_outcome_safely(db, u.id, c.id, disposition="not-a-real-disposition")


# ── 2. aggregating into the learned table ───────────────────────────────────

def test_refresh_aggregates_real_outcomes(db):
    u = _lawyer(db, practice_area="litigation")
    for i in range(5):
        c = _contact(db, u, key=f"li:{i}")
        _drafted(db, u, c, category="litigation_filed")
        db.commit()
        outcomes.record_draft_outcome(
            db, u.id, c.id,
            disposition="approved_as_is" if i < 4 else "discarded")

    stats = tax.refresh_from_outcomes(db)
    assert stats["outcomes"] == 5
    learned = tax.load_learned(db)
    assert learned[("litigation", "litigation_filed")] == (4, 5)


def test_generated_outcomes_never_enter_the_learned_table(db):
    """backend/demo/cohort.py writes dispositions too. Letting those in would
    mean synthetic outcomes quietly shaping real lawyers' rankings."""
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    row = _drafted(db, u, c, category="litigation_filed")
    db.commit()
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    prov.tag(db, row, provenance=prov.BASELINE, cohort_id="generated")
    db.commit()

    stats = tax.refresh_from_outcomes(db)
    assert stats["skipped_generated"] == 1
    assert stats["outcomes"] == 0
    assert tax.load_learned(db) == {}


def test_undecided_drafts_are_not_counted_as_rejections(db):
    """"Has not been looked at yet" is not evidence of disinterest. Counting
    it would produce a rate over invented negatives."""
    u = _lawyer(db, practice_area="litigation")
    for i in range(4):
        c = _contact(db, u, key=f"li:{i}")
        _drafted(db, u, c, category="litigation_filed")
    db.commit()
    c0 = db.execute(select(models.Contact)).scalars().first()
    outcomes.record_draft_outcome(db, u.id, c0.id, disposition="approved_as_is")

    tax.refresh_from_outcomes(db)
    assert tax.load_learned(db)[("litigation", "litigation_filed")] == (1, 1)


def test_refresh_is_idempotent(db):
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    _drafted(db, u, c, category="litigation_filed")
    db.commit()
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")

    tax.refresh_from_outcomes(db)
    tax.refresh_from_outcomes(db)
    assert tax.load_learned(db)[("litigation", "litigation_filed")] == (1, 1)
    assert db.query(models.SignalAffinity).count() == 1


# ── 3. affinity() actually consults it, without overfitting ─────────────────

def test_affinity_moves_toward_observed_behaviour(db):
    seed = tax.seed_affinity("corporate_ma", "litigation_filed")
    assert seed == 0.20
    learned = {("corporate_ma", "litigation_filed"): (90, 100)}
    got = tax.affinity("corporate_ma", "litigation_filed", learned=learned)
    assert got > 0.7, f"100 consistent observations barely moved the prior ({got})"


def test_a_single_observation_cannot_swing_the_prior(db):
    """The bug in the original recompute: `engaged / total` on ANY non-zero
    sample, so n=1 produced 0.0 or 1.0 and presented it as a measurement."""
    seed = tax.seed_affinity("litigation", "litigation_filed")
    assert seed == 0.90
    one_discard = tax.affinity("litigation", "litigation_filed",
                               learned={("litigation", "litigation_filed"): (0, 1)})
    assert one_discard > 0.8, f"one discarded draft crashed the prior to {one_discard}"
    assert one_discard < seed, "the observation should still move it, just barely"


def test_affinity_without_learned_data_is_exactly_the_seed(db):
    for area in tax.PRACTICE_AREAS:
        for cat in tax.ALL_SIGNAL_CATEGORIES:
            assert tax.affinity(area, cat) == tax.seed_affinity(area, cat)
            assert tax.affinity(area, cat, learned={}) == tax.seed_affinity(area, cat)


def test_the_seed_table_is_never_mutated(db):
    """The seed is the documented starting prior. Learning must not edit it."""
    before = {a: dict(c) for a, c in tax.SIGNAL_AFFINITY_SEED.items()}
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    _drafted(db, u, c, category="litigation_filed")
    db.commit()
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="discarded")
    tax.refresh_from_outcomes(db)
    tax.affinity("litigation", "litigation_filed", learned=tax.load_learned(db))
    assert tax.SIGNAL_AFFINITY_SEED == before


def test_ranking_trace_reports_learned_vs_seed_honestly(db):
    """A learned value and a seed guess are different claims; the trace has to
    say which, and on how much evidence."""
    from backend.demo import ranking_trace as rt

    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    signal = models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, occurred_at=datetime.now(UTC),
        source_type="activity_update", interaction_type="post", title="post",
        meta_json=json.dumps({"signal_category": "litigation_filed"}))
    db.add(signal)
    db.commit()

    seed_factor = rt._practice_fit(u, signal, learned={})
    assert "seed_table" in seed_factor.evidence["affinity_source"]

    learned_factor = rt._practice_fit(
        u, signal, learned={("litigation", "litigation_filed"): (30, 40)})
    src = learned_factor.evidence["affinity_source"]
    assert src.startswith("learned"), src
    assert "n=40" in src and "engaged=30" in src


def test_load_learned_survives_a_missing_table(db):
    """Mid-migration, ranking must fall back to the seed, not break."""
    models.SignalAffinity.__table__.drop(db.get_bind())
    assert tax.load_learned(db) == {}
    assert tax.affinity("litigation", "litigation_filed",
                        learned=tax.load_learned(db)) == 0.90


# ── the learned table moves on the write, not only on the sweep ─────────────

def test_recording_an_outcome_updates_the_learned_table_immediately(db):
    """Waiting for the daily sweep is invisible to the product but is exactly
    what someone watching the log wants to see. The write path applies its own
    delta; the sweep stays the reconciler."""
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    _drafted(db, u, c, category="litigation_filed")
    db.commit()

    assert tax.load_learned(db) == {}
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    assert tax.load_learned(db)[("litigation", "litigation_filed")] == (1, 1), \
        "the learned table did not move until the sweep ran"


def test_immediate_and_swept_counts_agree(db):
    """Incremental for latency, periodic recompute for correctness -- the two
    must not drift, or the number shown depends on when you looked."""
    u = _lawyer(db, practice_area="corporate_ma")
    for i in range(7):
        c = _contact(db, u, key=f"li:{i}")
        _drafted(db, u, c, category="acquisition_announced")
        db.commit()
        outcomes.record_draft_outcome(
            db, u.id, c.id,
            disposition="approved_as_is" if i % 3 else "discarded")

    incremental = tax.load_learned(db)
    tax.refresh_from_outcomes(db)
    assert tax.load_learned(db) == incremental, "sweep disagreed with the write path"


def test_the_immediate_update_also_excludes_generated_rows(db):
    """refresh_from_outcomes filters these out; its incremental counterpart
    has to as well, or the filter is only as good as the slower path."""
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    row = _drafted(db, u, c, category="litigation_filed")
    prov.tag(db, row, provenance=prov.BASELINE, cohort_id="generated")
    db.commit()

    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    assert tax.load_learned(db) == {}, "a generated outcome reached the learned table"


def test_idempotent_recording_cannot_double_count_the_learned_table(db):
    """The incremental path is only safe because a decided draft is never
    re-marked. Pin that the two facts stay connected."""
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    _drafted(db, u, c, category="litigation_filed")
    db.commit()

    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    assert tax.load_learned(db)[("litigation", "litigation_filed")] == (1, 1)


def test_the_sweep_corrects_an_inflated_count_downward(db):
    """The sweep is the reconciler, not an accumulator. If it could only add,
    a count inflated by the incremental write path -- a row later tagged as
    generated, an outcome whose interaction was deleted -- would persist
    forever and keep shaping rankings."""
    u = _lawyer(db, practice_area="litigation")
    c = _contact(db, u)
    _drafted(db, u, c, category="litigation_filed")
    db.commit()
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="approved_as_is")
    assert tax.load_learned(db)[("litigation", "litigation_filed")] == (1, 1)

    # The supporting outcome goes away.
    db.query(models.RelationshipInteraction).delete()
    db.commit()

    stats = tax.refresh_from_outcomes(db)
    assert stats["stale_pairs_dropped"] == 1
    assert tax.load_learned(db) == {}, "the sweep could not correct downward"


# ── production stores drafts differently from the demo cohort ───────────────

def _production_signal_with_draft(db, user, contact, category=None, draft="Hi there."):
    """How production ACTUALLY stores a draft: updates_engine.autodraft()
    stashes it on the detected-signal row's meta_json. There is no separate
    draft row and no "Drafted follow-up" title outside the demo cohort."""
    meta = {"draft": draft, "milestone_type": "acquisition"}
    if category:
        meta["signal_category"] = category
    row = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        occurred_at=datetime.now(UTC) - timedelta(hours=1),
        source_type="activity_update", interaction_type="new_post",
        title="New LinkedIn post", meta_json=json.dumps(meta))
    db.add(row)
    db.flush()
    return row


def test_a_production_draft_is_recognised_as_a_draft(db):
    """Matching only the demo title made the account summary report "nothing
    drafted" on the same screen as a pipeline line counting 250 stored
    autodrafts -- and left the outcome loop unable to fire on real data."""
    u = _lawyer(db, practice_area="corporate_ma")
    c = _contact(db, u)
    row = _production_signal_with_draft(db, u, c)
    db.commit()
    assert outcomes.is_drafted(row)
    assert outcomes.latest_undecided_draft(db, u.id, c.id) is not None


def test_a_signal_with_no_draft_is_not_a_draft(db):
    u = _lawyer(db)
    c = _contact(db, u)
    row = models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, occurred_at=datetime.now(UTC),
        source_type="activity_update", interaction_type="new_post",
        title="New LinkedIn post", meta_json=json.dumps({"milestone_type": "acquisition"}))
    db.add(row)
    db.commit()
    assert not outcomes.is_drafted(row)
    assert outcomes.latest_undecided_draft(db, u.id, c.id) is None


def test_sending_records_an_outcome_on_a_production_draft(db):
    """The whole learning loop, against production's storage shape."""
    u = _lawyer(db, practice_area="corporate_ma")
    c = _contact(db, u)
    _production_signal_with_draft(db, u, c, category="acquisition_announced",
                                  draft="Congrats on the deal.")
    db.commit()

    row = outcomes.record_draft_outcome(db, u.id, c.id, sent_body="Congrats on the deal.")
    assert row is not None, "a real production draft was invisible to the outcome loop"
    assert json.loads(row.meta_json)["disposition"] == "approved_as_is"
    assert tax.load_learned(db)[("corporate_ma", "acquisition_announced")] == (1, 1)


def test_the_sweep_aggregates_production_drafts_too(db):
    u = _lawyer(db, practice_area="corporate_ma")
    c = _contact(db, u)
    _production_signal_with_draft(db, u, c, category="acquisition_announced")
    db.commit()
    outcomes.record_draft_outcome(db, u.id, c.id, disposition="discarded")

    stats = tax.refresh_from_outcomes(db)
    assert stats["outcomes"] == 1, "the sweep could not see a production draft"
    assert tax.load_learned(db)[("corporate_ma", "acquisition_announced")] == (0, 1)
