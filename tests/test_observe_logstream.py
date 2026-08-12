"""tests/test_observe_logstream.py : the streaming execution log.

The load-bearing test here is evaluation_cohort_id's provenance filter. The
synthetic known-answer fixture cohort is written lazily by
synthetic_scenarios.setup() DURING a harness run, which makes it the newest
row in demo_provenance by construction -- so "newest cohort wins" silently
pointed replay/ablation/relationship_evaluation at 4 hand-built contacts and
they reported real arithmetic over the wrong dataset.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

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


def test_boot_events_builds_a_dataset_automatically_when_there_is_none(db):
    """A reader opening Observe with nothing seeded yet used to get four
    SKIPPED harnesses and a POST to go issue themselves -- self-heal instead:
    build the missing cohort right here, so the same boot's harnesses
    actually run against it."""
    events = list(logstream.boot_events(db))
    msgs = " | ".join(e["msg"] for e in events)
    assert "built automatically" in msgs
    assert "jurisdiction_regression" in msgs
    assert "SKIPPED" not in msgs
    assert logstream.evaluation_cohort_id(db) is not None, \
        "the built cohort must be findable on the next boot too"


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
            # Reported as coverage ("N/M lawyers"), never as "N/M pass".
            assert "lawyers" in e["msg"] and "pass" not in e["msg"]


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


def test_feedback_loop_status_claims_only_what_the_evidence_supports(db):
    """The single easiest thing to imply and not have.

    The taxonomy CAN learn now (agents/relationship/outcomes.py records a
    disposition on a real send or snooze, the affinity_refresh sweep
    aggregates it). That does not entitle the log to say it HAS. With no
    recorded outcomes it must say the affinity table is still the seed."""
    cohort.generate(db, n_lawyers=3, days=14, cohort_id="loop-cohort")
    user = db.execute(select(models.User)).scalars().first()
    joined = " | ".join(e["msg"] for e in logstream.feedback_loop_status(db, user))
    assert "CLOSED:" in joined and "historical_behavior" in joined
    assert "SEED ONLY" in joined, "claimed a learned taxonomy with nothing learned"
    assert "NOT counted as rejections" in joined


def test_feedback_loop_status_reports_a_learned_taxonomy_once_it_exists(db):
    """...and once outcomes ARE recorded, it says so with the counts."""
    from backend.agents.relationship import outcomes
    from backend.demo import signal_taxonomy as tax

    cohort.generate(db, n_lawyers=3, days=14, cohort_id="loop-cohort2")
    user = db.execute(select(models.User)).scalars().first()
    user.practice_area = "litigation"
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        occurred_at=datetime.now(timezone.utc),
        source_type="draft", interaction_type="message",
        title="Drafted follow-up (real)",
        meta_json=json.dumps({"signal_category": "litigation_filed",
                              "draft": "hello"})))
    db.commit()
    outcomes.record_draft_outcome(db, user.id, contact.id, sent_body="hello")
    tax.refresh_from_outcomes(db)

    joined = " | ".join(e["msg"] for e in logstream.feedback_loop_status(db, user))
    assert "CLOSED: signal_taxonomy.affinity() blends" in joined
    assert "seed 0.90" in joined, "the seed and the blended value are both shown"


def test_feedback_loop_status_counts_production_shaped_drafts_too(db):
    """A real account's draft lives on an activity_update row's meta_json,
    not a separately titled "Drafted follow-up" row (that's the demo cohort
    shape only). Matching only the demo title here made this section say
    "no recorded outcomes yet" for a lawyer whose account_events() line,
    moments earlier in the same trace, already reported a resolved one."""
    from backend.agents.relationship import outcomes

    user = models.User(email="prod2@example.com", is_demo=False, practice_area="litigation")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="p:2", name="C")
    db.add(contact)
    db.commit()
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        occurred_at=datetime.now(timezone.utc),
        source_type="activity_update", interaction_type="job_change",
        title="Changed roles",
        meta_json=json.dumps({"signal_category": "litigation_filed", "draft": "hello"})))
    db.commit()
    outcomes.record_draft_outcome(db, user.id, contact.id, sent_body="hello")

    joined = " | ".join(e["msg"] for e in logstream.feedback_loop_status(db, user))
    assert "CLOSED: 1 resolved outcomes" in joined


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


def test_draft_narrates_even_when_the_jurisdiction_needs_no_disclosure_label(db):
    """Gap: draft_events used to run ONLY inside jurisdiction_events, gated
    behind requires_disclosure_label -- so a CA-barred lawyer (CA's rule has
    requires_disclosure_label=False) never saw ANY LLM/drafting activity in a
    contact-click trace. contact_events now resolves the draft unconditionally
    before jurisdiction runs."""
    cohort.generate(db, n_lawyers=3, days=30, cohort_id="ca-cohort")
    row = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    user = db.get(models.User, row.actor_user_id)
    user.bar_jurisdiction = "CA"
    db.commit()
    contact = db.get(models.Contact, row.contact_id)

    events = list(logstream.contact_events(db, user, contact))
    joined = " | ".join(e["msg"] for e in events)

    assert "resolving this contact's draft" in joined
    assert "no advertising label on written solicitation" in joined
    assert "no label check needed for CA" in joined
    # and the draft resolution itself actually ran (stored autodraft path,
    # since cohort.generate() writes one onto meta_json["draft"]).
    assert "reusing stored autodraft" in joined or "draft resolved via" in joined


def test_jurisdiction_events_standalone_still_resolves_its_own_draft(db):
    """`resolved=None` (the default) must keep jurisdiction_events
    independently callable -- existing standalone tests rely on this."""
    cohort.generate(db, n_lawyers=3, days=30, cohort_id="standalone-ca")
    row = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    user = db.get(models.User, row.actor_user_id)
    user.bar_jurisdiction = "CA"
    db.commit()
    contact = db.get(models.Contact, row.contact_id)

    joined = " | ".join(e["msg"] for e in logstream.jurisdiction_events(db, user, contact))
    assert "no advertising label on written solicitation" in joined
    assert "draft resolved via" in joined or "reusing stored autodraft" in joined


def test_jurisdiction_events_reuses_a_passed_in_resolved_draft(db, monkeypatch):
    """When `resolved` is supplied, jurisdiction_events must NOT re-run
    draft_events -- it reuses the caller's already-resolved draft."""
    cohort.generate(db, n_lawyers=3, days=30, cohort_id="reuse-cohort")
    row = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    user = db.get(models.User, row.actor_user_id)
    user.bar_jurisdiction = "NY"
    db.commit()
    contact = db.get(models.Contact, row.contact_id)

    calls = []

    def counting_draft_events(*a, **k):
        calls.append(1)
        yield from ()

    monkeypatch.setattr(logstream, "draft_events", counting_draft_events)

    resolved = {"text": "Attorney Advertising: a hand-off draft.",
               "source": "test:hand-off", "interaction_id": None}
    events = list(logstream.jurisdiction_events(db, user, contact, resolved=resolved))

    assert calls == [], "jurisdiction_events must not re-resolve a draft it was already given"
    joined = " | ".join(e["msg"] for e in events)
    assert "test:hand-off" in joined


def test_the_contact_trace_reports_only_what_this_click_did(db, monkeypatch):
    """Modal's on/off state, the judge model's role and per-worker cache
    hashes are all true, and none of them is something a CONTACT CLICK did.
    Repeating them on every trace buried the eight pipeline stages and the
    scoring arithmetic that are. Modal is still reported where it is a fact
    about the run -- see test_observe_askprobe's Modal coverage on the ask
    narration, which is the path Modal could actually affect."""
    monkeypatch.setenv("USE_MODAL", "1")
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="modal-cohort")
    user = db.execute(select(models.User)).scalars().first()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()

    joined = " | ".join(e["msg"] for e in logstream.contact_events(db, user, contact))
    for noise in ("Modal batch dispatch", "JUDGE_MODEL=", "assess cache", "draft cache"):
        assert noise not in joined, f"{noise!r} is not something this click did"

    # ...and what a reader actually came for is still all there.
    for signal in ("running execution pipeline", "factor × weight breakdown",
                    "opportunity_score =", "ablation loop", "VERDICT:"):
        assert signal in joined, f"trimming removed {signal!r}"


def test_judge_model_status_distinguishes_live_infra_from_dead_code(db):
    joined = " | ".join(e["msg"] for e in logstream.judge_model_status())
    from backend.agents import llm
    assert f"JUDGE_MODEL={llm.JUDGE_MODEL}" in joined
    assert "real cheap-tier infra" in joined
    assert f"MODEL={llm.MODEL}" in joined
    assert "judge_relevance_batch" in joined
    assert "verified dead code" in joined


def test_cache_status_events_reports_a_real_assess_and_draft_hit(db):
    from backend.agents.relationship import book as book_agent
    from backend.routes.book import _find_contact_fast

    cohort.generate(db, n_lawyers=2, days=14, cohort_id="cachehit-cohort")
    user = db.execute(select(models.User)).scalars().first()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()

    cdict = _find_contact_fast(db, user, str(contact.id))
    assert cdict is not None
    akey = book_agent._assess_key(cdict)
    dkey = book_agent._draft_key(cdict, "catching up", "linkedin_dm")
    book_agent._assess_cache[akey] = (time.time(), {"health": "warm"}, None)
    book_agent._draft_cache[dkey] = (time.time(), {"body": "cached draft"})
    try:
        joined = " | ".join(e["msg"] for e in
                            logstream.cache_status_events(db, user, contact, "linkedin_dm"))
        assert "assess cache: HIT" in joined
        assert "draft cache: HIT" in joined
        assert "this worker only" in joined
    finally:
        book_agent._assess_cache.pop(akey, None)
        book_agent._draft_cache.pop(dkey, None)


def test_cache_status_events_reports_a_real_miss(db):
    from backend.agents.relationship import book as book_agent

    cohort.generate(db, n_lawyers=2, days=14, cohort_id="cachemiss-cohort")
    user = db.execute(select(models.User)).scalars().first()
    contact = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().first()

    book_agent._assess_cache.clear()
    book_agent._draft_cache.clear()
    joined = " | ".join(e["msg"] for e in
                        logstream.cache_status_events(db, user, contact, "linkedin_dm"))
    assert "assess cache: MISS" in joined
    assert "draft cache: MISS" in joined
    assert "another worker may already hold" in joined


def test_boot_reports_the_updates_pipeline_before_the_harnesses(db):
    """The feed on screen raises "what has the system been doing"; the
    harnesses answer "does it hold up under evaluation". The pipeline state
    must come first, or the harness numbers read as the whole story."""
    cohort.generate(db, n_lawyers=4, days=30, cohort_id="flow-cohort")
    user = db.execute(select(models.User)).scalars().first()

    msgs = [e["msg"] for e in logstream.boot_events(db, user)]
    joined = " | ".join(msgs)

    assert "updates pipeline -- detect → target → draft" in joined
    account_at = next(i for i, m in enumerate(msgs) if m == "account")
    pipeline_at = next(i for i, m in enumerate(msgs) if "updates pipeline" in m)
    harness_at = next(i for i, m in enumerate(msgs) if "jurisdiction_regression" in m)
    assert account_at < pipeline_at < harness_at, \
        "boot log order must be account -> pipeline -> evaluation"


def test_harnesses_do_not_dominate_the_boot_log(db):
    """The rebalance. Each harness gets ONE line carrying its headline number;
    three lines each made them roughly half the log and pushed the account
    state a reader actually opens the page for off the top of the screen."""
    cohort.generate(db, n_lawyers=6, days=30, cohort_id="balance-cohort")
    user = db.execute(select(models.User)).scalars().first()
    events = list(logstream.boot_events(db, user))

    for harness_id in ("jurisdiction_regression", "historical_replay", "ablation",
                        "relationship_evaluation", "signal_library_evaluation"):
        result_lines = [e for e in events
                        if e.get("harness") == harness_id and e.get("kind")]
        assert len(result_lines) == 1, f"{harness_id} emitted {len(result_lines)} result lines"

    # ...and the headline number still survives on that one line.
    by_id = {e["harness"]: e["msg"] for e in events if e.get("kind")}
    assert "leakage=" in by_id["historical_replay"]
    assert "ndcg@5" in by_id["ablation"]
    assert "accuracy=" in by_id["signal_library_evaluation"]

    harness_lines = sum(1 for e in events if e.get("harness"))
    assert harness_lines < len(events) / 2, \
        f"harnesses are {harness_lines} of {len(events)} lines -- still dominating"


def test_account_section_reports_real_state(db):
    """The section that answers 'what is this doing for ME' -- read from real
    rows, and honest when a field is unset."""
    cohort.generate(db, n_lawyers=6, days=30, cohort_id="acct-cohort")
    user = db.execute(select(models.User)).scalars().first()
    user.bar_jurisdiction = None
    db.commit()

    joined = " | ".join(e["msg"] for e in logstream.account_events(db, user))
    assert user.email in joined
    assert "bar=NOT SET" in joined, "an unset jurisdiction must be stated, not omitted"
    assert "book:" in joined and "contacts" in joined
    assert "outreach:" in joined


def test_account_section_says_so_when_there_is_no_signed_in_user(db):
    joined = " | ".join(e["msg"] for e in logstream.account_events(db, None))
    assert "no signed-in account" in joined


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


def test_a_cohort_with_no_tagged_lawyer_is_not_chosen_as_the_dataset(db):
    """The exact failure that shipped: an earlier seed tagged contacts and
    interactions but not the User row. Every harness resolves lawyers from a
    table_name=="users" tag, so that cohort evaluated 0/0 -- and being newest,
    it shadowed any working cohort behind it."""
    from backend.demo import provenance as prov

    good = cohort.generate(db, n_lawyers=3, days=14, cohort_id="good-cohort")

    # A newer cohort with contacts but no user tag -- the trap. A fresh
    # contact, since a row may only carry one provenance tag.
    owner = db.execute(select(models.User)).scalars().first()
    untagged = models.Contact(user_id=owner.id, primary_identity_key="ul:1",
                              name="Untagged")
    db.add(untagged)
    db.flush()
    prov.tag(db, untagged, provenance=prov.BASELINE, cohort_id="userless-cohort")
    db.commit()

    newest = db.execute(select(models.DemoProvenance.cohort_id)
                        .order_by(models.DemoProvenance.id.desc()).limit(1)).scalar_one()
    assert newest == "userless-cohort"
    assert logstream.evaluation_cohort_id(db) == good, \
        "a cohort with no tagged lawyer shadowed a working one"


def test_a_missing_dataset_is_built_rather_than_left_unexplained(db):
    """0/0 with no explanation was the worst version of this screen; the fix
    is to not leave it in that state at all."""
    events = list(logstream.boot_events(db))
    joined = " | ".join(e["msg"] for e in events)
    assert "built automatically" in joined
    assert logstream.evaluation_cohort_id(db) is not None


# ── the numbers must say whether they are evidence or a wiring check ────────

def test_generated_labels_are_declared_as_circular(db):
    """backend/demo/cohort.py draws the engagement label from
    p_engage = 0.25 + 0.65 * affinity(practice_area, category) -- the same
    seed table practice_fit scores with. An NDCG lift over that shows the
    ranker recovering the generator's own rule. Printing 0.77 without saying
    so invites reading a self-fulfilling number as a performance claim."""
    cohort.generate(db, n_lawyers=6, days=30, cohort_id="circ-cohort")
    user = db.execute(select(models.User)).scalars().first()
    events = list(logstream.boot_events(db, user))
    joined = " | ".join(e["msg"] for e in events)

    assert "labels: GENERATED" in joined
    assert "same seed table practice_fit scores with" in joined
    assert "not evidence" in joined.lower()

    # ...and on the individual lines, so one screenshotted line is not
    # misleading on its own.
    by_id = {e["harness"]: e["msg"] for e in events if e.get("kind")}
    for harness_id in ("ablation", "relationship_evaluation", "historical_replay"):
        assert "labels=generated" in by_id[harness_id], \
            f"{harness_id} reported a label-dependent number with no provenance"


def test_label_free_harnesses_are_not_tagged_as_circular(db):
    """jurisdiction_regression checks hand-written rule expectations against
    the real gate and signal_library_evaluation grades a reference classifier
    -- neither depends on an outcome label, so neither may carry the caveat."""
    cohort.generate(db, n_lawyers=6, days=30, cohort_id="circ-cohort2")
    user = db.execute(select(models.User)).scalars().first()
    by_id = {e["harness"]: e["msg"] for e in logstream.boot_events(db, user)
             if e.get("kind")}
    for harness_id in ("jurisdiction_regression", "signal_library_evaluation"):
        assert "labels=" not in by_id[harness_id], \
            f"{harness_id} does not use outcome labels; the caveat misapplies"


def test_observed_labels_are_declared_as_evidence(db):
    """The inverse. Real recorded dispositions are independent of the factors
    being scored, and the log has to distinguish that case or the caveat is
    just boilerplate."""
    user = models.User(email="real@example.com", name="Real", is_demo=False,
                       practice_area="litigation")
    db.add(user)
    db.flush()
    prov.tag(db, user, provenance=prov.OBSERVED, cohort_id="obs-cohort")
    db.commit()

    assert logstream.label_provenance(db, "obs-cohort") == "observed"
    level, note = logstream.circularity_note("observed")
    assert level == logstream.OK
    assert "real recorded dispositions" in note
    assert "independent of the factors being scored" in note


def test_a_mixed_cohort_is_not_reported_as_either(db):
    """Blending generated and real labels in one cohort cannot be read as
    either, and saying "observed" would be the more flattering lie."""
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="mixed-cohort")
    user = models.User(email="r2@example.com", name="R2", is_demo=False)
    db.add(user)
    db.flush()
    prov.tag(db, user, provenance=prov.OBSERVED, cohort_id="mixed-cohort")
    db.commit()

    assert logstream.label_provenance(db, "mixed-cohort") == "mixed"
    level, note = logstream.circularity_note("mixed")
    assert level == logstream.WARN and "cannot be cleanly interpreted" in note


def test_a_near_chance_agreement_rate_says_so(db):
    """historical_replay's agreement is a binary-outcome rate: 0.4742 on one
    run and 0.5292 on the next is noise, and printing it bare invites reading
    noise as a finding. leakage/reconstruction are invariants and stay bare."""
    class _R:
        metrics = {"temporal_leakage_violations": 0, "reconstruction_failures": 0,
                   "prediction_outcome_agreement_rate": 0.5292}
    assert "≈chance" in logstream._harness_summary("historical_replay", _R())

    class _S:
        metrics = {"temporal_leakage_violations": 0, "reconstruction_failures": 0,
                   "prediction_outcome_agreement_rate": 0.83}
    assert "≈chance" not in logstream._harness_summary("historical_replay", _S())
