"""tests/test_observe_logstream.py : the streaming execution log.

The load-bearing test here is evaluation_cohort_id's provenance filter. The
synthetic known-answer fixture cohort is written lazily by
synthetic_scenarios.setup() DURING a harness run, which makes it the newest
row in demo_provenance by construction -- so "newest cohort wins" silently
pointed replay/ablation/relationship_evaluation at 4 hand-built contacts and
they reported real arithmetic over the wrong dataset.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort, provenance as prov
from backend.observe import logstream
from backend.observe.harnesses import synthetic_scenarios


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


def test_evaluation_cohort_id_is_none_on_an_empty_database(db):
    assert logstream.evaluation_cohort_id(db) is None


def test_evaluation_cohort_id_picks_the_baseline_cohort(db):
    cohort_id = cohort.generate(db, n_lawyers=3, days=14, cohort_id="real-cohort")
    assert logstream.evaluation_cohort_id(db) == cohort_id


def test_synthetic_fixtures_never_become_the_evaluation_dataset(db):
    """The regression: run the synthetic harness AFTER generating a real
    cohort, so its fixture rows are strictly newer, and confirm the
    evaluation dataset is still the generated cohort."""
    cohort.generate(db, n_lawyers=3, days=14, cohort_id="real-cohort")
    synthetic_scenarios.run(db)

    newest_any = db.execute(
        select(models.DemoProvenance.cohort_id)
        .order_by(models.DemoProvenance.id.desc()).limit(1)
    ).scalar_one()
    assert newest_any == synthetic_scenarios.COHORT_ID  # the trap

    assert logstream.evaluation_cohort_id(db) == "real-cohort"


def test_boot_events_stream_real_harness_results(db):
    cohort.generate(db, n_lawyers=4, days=14, cohort_id="boot-cohort")
    events = list(logstream.boot_events(db))
    assert events

    for e in events:
        assert set(("ts", "level", "src", "msg")) <= set(e)
        assert e["level"] in ("info", "ok", "warn", "error", "step")

    msgs = " | ".join(e["msg"] for e in events)
    assert "cohort_id=boot-cohort" in msgs
    for harness_id in ("jurisdiction_regression", "historical_replay", "ablation",
                        "relationship_evaluation", "signal_library_evaluation"):
        assert harness_id in msgs, f"{harness_id} never ran"
    # synthetic_scenarios is a correctness check against hand-built fixtures,
    # not a read of this account's real state -- deliberately excluded from
    # the boot sequence (still reachable on demand via its own harness route).
    assert "synthetic_scenarios" not in msgs


def test_boot_events_warn_and_skip_when_there_is_no_dataset(db):
    events = list(logstream.boot_events(db))
    msgs = " | ".join(e["msg"] for e in events)
    assert "no demo cohort in this database" in msgs
    # The one dataset-free harness still runs; the four data-driven ones skip.
    assert "jurisdiction_regression" in msgs
    assert "SKIPPED" in msgs


def test_coverage_harnesses_are_not_reported_as_failures(db):
    """ablation / relationship_evaluation use cases_passed as COVERAGE
    (lawyers with a resolved outcome), not pass/fail -- reporting 9/10 as a
    red failure line misreports a healthy run."""
    cohort.generate(db, n_lawyers=6, days=30, cohort_id="cov-cohort")
    events = list(logstream.boot_events(db))
    for e in events:
        if e.get("harness") in ("ablation", "relationship_evaluation") and e.get("kind"):
            assert e["kind"] == "coverage"
            assert e["level"] != "error"
            assert "lawyers with a resolved outcome" in e["msg"]


def test_contact_events_show_the_scoring_arithmetic_and_jurisdiction_checks(db):
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="trace-cohort")
    user = db.execute(select(models.User)).scalars().first()
    user.bar_jurisdiction = "NY"
    db.commit()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()

    events = list(logstream.contact_events(db, user, contact))
    msgs = [e["msg"] for e in events]
    joined = " | ".join(msgs)

    # the factor-by-factor arithmetic, not just a final score
    assert "factor × weight breakdown" in joined
    assert "opportunity_score =" in joined
    for factor in ("signal_relevance", "practice_fit", "relationship_strength"):
        assert factor in joined

    # the real NY rule set, walked check by check
    assert "jurisdiction: NY" in joined
    for check in ("[1/4] exemption", "[2/4] realtime channel",
                   "[3/4] cooldown", "[4/4] volume cap"):
        assert check in joined
    assert "VERDICT:" in joined


def test_jurisdiction_events_report_an_unset_bar_jurisdiction_honestly(db):
    user = models.User(email="nojur@example.com", name="No Jurisdiction", is_demo=False)
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="nj:1", name="C")
    db.add(contact)
    db.commit()

    joined = " | ".join(e["msg"] for e in logstream.jurisdiction_events(db, user, contact))
    assert "bar_jurisdiction is NOT SET" in joined
    assert "fail-closed" in joined


def test_generated_signal_rows_carry_a_draft_like_production_autodraft(db):
    """production's updates_engine.autodraft() stores the composed follow-up
    on meta_json["draft"]; a generated signal row without it is not
    schema-faithful and every consumer of that key silently finds nothing."""
    import json
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="draft-cohort")
    rows = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().all()
    assert rows
    for r in rows:
        assert json.loads(r.meta_json or "{}").get("draft"), "generated signal has no draft body"


def test_jurisdiction_quotes_the_real_draft_and_flags_a_missing_label(db):
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="label-cohort")
    row = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    user = db.get(models.User, row.actor_user_id)
    user.bar_jurisdiction = "NY"
    db.commit()
    contact = db.get(models.Contact, row.contact_id)

    events = list(logstream.jurisdiction_events(db, user, contact))
    joined = " | ".join(e["msg"] for e in events)

    # the real draft body is quoted line by line
    assert " L1 " in joined or "L1  " in joined
    # NY's label requirement is a real, surfaced finding on this draft
    assert "Attorney Advertising" in joined
    assert "MISSING" in joined
    # and the missing citation is surfaced rather than invented
    assert "NO CITATION recorded for the NY entry" in joined


def test_feedback_loop_status_does_not_claim_the_taxonomy_learns(db):
    """The single easiest thing to imply and not have."""
    cohort.generate(db, n_lawyers=3, days=14, cohort_id="loop-cohort")
    user = db.execute(select(models.User)).scalars().first()
    joined = " | ".join(e["msg"] for e in logstream.feedback_loop_status(db, user))
    assert "CLOSED:" in joined and "historical_behavior" in joined
    assert "OPEN:" in joined
    assert "not called anywhere in production" in joined


def test_ranking_candidates_are_bounded_on_a_large_book(db):
    """A real book of hundreds of contacts scored every one of them, several
    queries each, with nothing emitted until all eight stages finished --
    which reads as a hang. The candidate set is bounded and the subject
    contact is always included."""
    from backend.observe import pipeline
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="big-cohort")
    user = db.execute(select(models.User)).scalars().first()
    base = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    for i in range(300):
        db.add(models.Contact(user_id=user.id, primary_identity_key=f"big:{i}", name=f"C{i}"))
    db.commit()

    subject = base[0]
    candidates, total = pipeline.resolve_candidates(db, user, subject, None)
    assert total > pipeline.MAX_RANKING_CANDIDATES
    assert len(candidates) <= pipeline.MAX_RANKING_CANDIDATES + 1
    assert subject in candidates, "the contact being traced must always be scored"


def test_iter_stages_yields_incrementally(db):
    """compute_pipeline_trace consumes iter_stages, so a streaming caller
    gets each stage as it completes rather than all eight at the end."""
    from backend.observe import pipeline
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="iter-cohort")
    user = db.execute(select(models.User)).scalars().first()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()

    it = pipeline.iter_stages(db, user, contact)
    first = next(it)
    assert first.name == "ingestion"          # arrives before the rest run
    names = [first.name] + [s.name for s in it]
    assert names == list(pipeline.STAGE_ORDER)


def test_seeded_cohort_survives_the_demo_purge_sweep(db):
    """cohort.generate() populates last_login_at and sets is_demo=True, so
    every seeded lawyer matched the stale-demo-user sweep and the whole
    evaluation dataset would vanish once past DEMO_TTL_HOURS. A per-visit
    demo workspace should still be reaped; a provenance-tagged evaluation
    cohort should not."""
    from datetime import datetime, timedelta, timezone
    from backend.routes.demo import _cleanup_stale_demo_users

    cohort.generate(db, n_lawyers=3, days=14, cohort_id="purge-cohort")
    seeded = db.execute(select(models.User).where(
        models.User.email.like("demo-lawyer-%"))).scalars().all()
    assert seeded
    seeded_ids = {u.id for u in seeded}

    # Age every demo user well past the TTL.
    old = datetime.now(timezone.utc) - timedelta(hours=500)
    for u in seeded:
        u.last_login_at = old

    # A genuine per-visit demo user, same flag, no provenance tag.
    visitor = models.User(email="visitor@demo.surpluslayer.com", is_demo=True,
                          last_login_at=old)
    db.add(visitor)
    db.commit()
    visitor_id = visitor.id

    _cleanup_stale_demo_users(db, limit=100)

    surviving = {u.id for u in db.execute(select(models.User)).scalars().all()}
    assert seeded_ids <= surviving, "seeded evaluation cohort was purged"
    assert visitor_id not in surviving, "per-visit demo user should still be reaped"


def test_draft_events_report_the_heuristic_path_when_no_model_key(db, monkeypatch):
    """No ANTHROPIC_API_KEY must be stated outright, not silently papered over
    with a fallback draft that looks like model output."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="nokey")
    user = db.execute(select(models.User)).scalars().first()
    contact = models.Contact(user_id=user.id, primary_identity_key="nk:1", name="No Key")
    db.add(contact)
    db.commit()

    out = {}
    joined = " | ".join(e["msg"] for e in logstream.draft_events(db, user, contact, out))
    assert "ANTHROPIC_API_KEY is not set" in joined
    assert "heuristic" in joined
    assert out["text"]
    assert "heuristic" in out["source"]


def test_draft_events_stream_live_model_progress(db, monkeypatch):
    """The LLM call is the slowest, least deterministic step in the trace and
    was previously invisible. Only the token SOURCE is stubbed -- the
    instrumentation around it (first-token latency, delta count, model name,
    total) is the real code under test."""
    import time as _t
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from backend.agents.relationship.pipeline.compose import drafting

    body = "Congratulations on the acquisition. " * 6

    def fake_stream(db_, user_id, contact_, *, reason, channel="email", intent=None):
        for i in range(0, len(body), 8):
            _t.sleep(0.001)
            yield body[i:i + 8]

    monkeypatch.setattr(drafting, "compose_stream", fake_stream)

    cohort.generate(db, n_lawyers=2, days=14, cohort_id="livedraft")
    user = db.execute(select(models.User)).scalars().first()
    contact = models.Contact(user_id=user.id, primary_identity_key="lv:1", name="Live Draft")
    db.add(contact)
    db.commit()

    out = {}
    events = list(logstream.draft_events(db, user, contact, out))
    joined = " | ".join(e["msg"] for e in events)

    assert "streaming token-by-token" in joined
    assert "first token after" in joined
    assert "streaming…" in joined, "no incremental progress while tokens arrived"
    assert "draft composed:" in joined
    assert out["text"].strip() == body.strip()
    assert "compose_stream" in out["source"]


def test_draft_events_prefer_a_stored_autodraft_over_a_model_call(db):
    """A stored autodraft is the message the product already shows; recomposing
    it would spend a model call and could show text the product never used."""
    cohort.generate(db, n_lawyers=3, days=30, cohort_id="stored")
    row = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    user = db.get(models.User, row.actor_user_id)
    contact = db.get(models.Contact, row.contact_id)

    out = {}
    joined = " | ".join(e["msg"] for e in logstream.draft_events(db, user, contact, out))
    assert "reusing stored autodraft" in joined
    assert "no model call needed" in joined
    assert out["interaction_id"] is not None


def test_boot_reports_the_updates_pipeline_before_the_harnesses(db):
    """The feed on screen raises "what has the system been doing"; the
    harnesses answer "does it hold up under evaluation". The pipeline state
    must come first, or the harness numbers read as the whole story."""
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="flow-cohort")
    user = db.execute(select(models.User)).scalars().first()

    msgs = [e["msg"] for e in logstream.boot_events(db, user)]
    joined = " | ".join(msgs)

    assert "updates pipeline -- detect → target → draft" in joined
    pipeline_at = next(i for i, m in enumerate(msgs) if "updates pipeline" in m)
    harness_at = next(i for i, m in enumerate(msgs) if "running jurisdiction_regression" in m)
    assert pipeline_at < harness_at, "harnesses reported before the pipeline they evaluate"


def test_updates_pipeline_reports_cadence_funnel_and_recent_signals(db):
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="pipe-cohort")
    user = db.execute(select(models.User)).scalars().first()

    joined = " | ".join(e["msg"] for e in logstream.updates_pipeline_events(db, user))

    assert "cadence: sweep every" in joined and "re-check vip every" in joined
    # due-vs-cached split, so unchanged contacts are visibly not re-fetched
    assert "due for re-check now" in joined
    assert "still inside their tier window (cached, not re-fetched)" in joined
    # the detect -> draft -> outcome funnel
    assert "carry a stored autodraft" in joined
    assert "have a recorded outcome" in joined
    assert "by kind:" in joined
    # per-signal detail with the real elapsed time
    assert "detected" in joined and "drafted" in joined


def test_updates_pipeline_never_triggers_a_sweep(db, monkeypatch):
    """Opening a debugger must not fire BrightData/Exa fetches or autodrafts
    -- that would spend money and mutate data as a side effect of a page
    load. This asserts the read-only contract directly."""
    from backend.agents.relationship import updates_engine as ue

    called = []
    monkeypatch.setattr(ue, "run_sweep",
                        lambda *a, **k: called.append("run_sweep"), raising=False)
    monkeypatch.setattr(ue, "autodraft",
                        lambda *a, **k: called.append("autodraft"), raising=False)

    cohort.generate(db, n_lawyers=3, days=14, cohort_id="nosweep")
    user = db.execute(select(models.User)).scalars().first()
    list(logstream.updates_pipeline_events(db, user))

    assert called == [], f"page load triggered live work: {called}"


def test_updates_pipeline_survives_a_missing_scheduler_claims_table(db):
    """scheduler_claims doesn't exist until the first sweep runs; that must
    be reported, not raised."""
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="noclaims")
    user = db.execute(select(models.User)).scalars().first()
    events = list(logstream.updates_pipeline_events(db, user))
    joined = " | ".join(e["msg"] for e in events)
    assert "no sweep has ever run in this database" in joined or "last ran" in joined


def test_scheduler_claims_read_failure_is_not_reported_as_never_run(db, monkeypatch):
    """A broken query and a not-yet-created table are different facts.

    The old handler caught every exception and printed "no sweep has run",
    so a genuine read failure was rendered as a confident (and false)
    statement about the scheduler. In a panel whose whole premise is that
    its lines are checkable, that is the worst kind of bug.
    """
    from sqlalchemy.exc import OperationalError

    cohort.generate(db, n_lawyers=2, days=14, cohort_id="brokenclaims")
    user = db.execute(select(models.User)).scalars().first()

    real_execute = db.execute

    def boom(stmt, *a, **kw):
        if "scheduler_claims" in str(stmt):
            raise OperationalError("SELECT ...", {}, Exception("connection reset"))
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", boom)
    joined = " | ".join(e["msg"] for e in logstream.updates_pipeline_events(db, user))

    assert "no sweep has ever run" not in joined, "a read failure was reported as fact"
    assert "UNVERIFIED" in joined
    assert "connection reset" in joined


def test_missing_table_detector_separates_the_two_cases():
    from sqlalchemy.exc import OperationalError, ProgrammingError

    sqlite_missing = OperationalError("q", {}, Exception("no such table: scheduler_claims"))
    pg_missing = ProgrammingError(
        "q", {}, Exception('relation "scheduler_claims" does not exist'))
    broken = OperationalError("q", {}, Exception("connection reset by peer"))

    assert logstream._is_missing_table(sqlite_missing)
    assert logstream._is_missing_table(pg_missing)
    assert not logstream._is_missing_table(broken)


def test_empty_scheduler_claims_table_is_reported(db, monkeypatch):
    """Table present but no rows: the old loop yielded nothing at all, so the
    reader saw silence where a real state exists."""
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="emptyclaims")
    user = db.execute(select(models.User)).scalars().first()

    real_execute = db.execute

    class _Empty:
        def all(self):
            return []

    def empty_claims(stmt, *a, **kw):
        if "scheduler_claims" in str(stmt):
            return _Empty()
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", empty_claims)
    joined = " | ".join(e["msg"] for e in logstream.updates_pipeline_events(db, user))
    assert "exists but is empty" in joined
