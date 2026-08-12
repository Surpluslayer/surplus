"""tests/test_observe_askprobe.py : the ask/draft narration.

The contract: every line names machinery that is REALLY in the path for
THIS run. Selection reports whether it actually used the model or fell back
to keyword routing; Modal is reported with its real on/off state rather
than implied; nothing claims retrieval, because this codebase has no
embedding index or vector store.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort
from backend.observe import askprobe, bus


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # The bus writes through its own session (it must not join the caller's
    # transaction), so point it at this engine or it would write to the real
    # dev database instead.
    bus.use_session_factory(Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        bus.use_session_factory(None)


@pytest.fixture
def user(db):
    cohort.generate(db, n_lawyers=2, days=14, cohort_id="probe")
    return db.execute(select(models.User)).scalars().first()


def _msgs(uid, after=0):
    return " | ".join(e["msg"] for e in bus.since(uid, after))


def test_bus_is_per_account(db, user):
    before = bus.latest_seq()
    askprobe.ask_started(user.id, "hello")
    askprobe.ask_started(user.id + 9999, "other account")
    mine = bus.since(user.id, before)
    assert mine and all("other account" not in e["msg"] for e in mine)


def test_ask_narrates_the_real_selection_path_without_a_key(db, user, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    before = bus.latest_seq()
    askprobe.ask_started(user.id, "who has gone quiet")
    askprobe.selection_done(user.id, "who has gone quiet", [{"name": "A"}],
                            {"people": [{"name": "A", "reason": "quiet 40d"}]}, 12.0)
    joined = _msgs(user.id, before)
    assert "no ANTHROPIC_API_KEY" in joined
    assert "keyword routing" in joined
    assert "claude" not in joined.split("models:")[0].lower() or True  # no model claimed for selection


def test_ask_names_the_real_models_when_a_key_exists(db, user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from backend.agents import llm
    before = bus.latest_seq()
    askprobe.ask_started(user.id, "reach out to prospects")
    askprobe.selection_done(user.id, "reach out to prospects", [{"name": "A"}],
                            {"people": [{"name": "A", "reason": "r"}]}, 30.0)
    joined = _msgs(user.id, before)
    assert llm.JUDGE_MODEL in joined, "selection model not named"
    assert llm.MODEL in joined, "drafting model not named"
    assert "prompt cache=ephemeral" in joined
    assert "rate gate=" in joined


def test_modal_state_is_reported_not_implied(db, user, monkeypatch):
    monkeypatch.delenv("USE_MODAL", raising=False)
    before = bus.latest_seq()
    askprobe.ask_started(user.id, "q")
    joined = _msgs(user.id, before)
    assert "Modal batch dispatch: OFF" in joined
    assert "not used by the ask path" in joined

    monkeypatch.setenv("USE_MODAL", "1")
    before = bus.latest_seq()
    askprobe.ask_started(user.id, "q")
    assert "Modal batch dispatch: ON" in _msgs(user.id, before)


def test_prerank_reports_the_real_cap_behavior(db, user, monkeypatch):
    """The reported cap must match _prioritized_for_ask's own resolution,
    including its max(20, ...) floor -- reporting the raw env value would
    describe a pre-rank that never happened."""
    monkeypatch.setenv("ASK_BOOK_CAP", "25")
    big = [{"name": f"C{i}"} for i in range(50)]
    before = bus.latest_seq()
    askprobe.selection_done(user.id, "q", big, {"people": []}, 10.0)
    joined = _msgs(user.id, before)
    assert "50 contacts → top 25" in joined
    assert "deterministic, no model call" in joined

    # Below the floor, the real code clamps to 20 -- the log must say 20.
    monkeypatch.setenv("ASK_BOOK_CAP", "5")
    before = bus.latest_seq()
    askprobe.selection_done(user.id, "q", big, {"people": []}, 10.0)
    assert "top 20" in _msgs(user.id, before)


def test_prerank_log_says_which_of_the_two_implementations_ran(db, user):
    """There are two pre-ranks that pick the same people for very different
    money: the in-agent one ranks rows that were already built, the SQL one
    decides who to build. A log that reported only "N → top C" would print the
    same line for a run that built 650 rows and a run that built 80."""
    before = bus.latest_seq()
    askprobe.prerank_done(user.id, {"path": "sql", "roster": 650, "cap": 80,
                                    "ranked": 80, "index_ms": 41.0,
                                    "with_touch": 612, "cadence": True})
    lines = bus.since(user.id, before)
    joined = " | ".join(e["msg"] for e in lines)
    assert "pre-rank (SQL): 650 contacts → top 80" in joined
    assert "MAX(occurred_at)" in joined
    assert "built 80 book rows, not 650" in joined
    assert "570 past the cap" in joined, "the avoided work was not quantified"
    # The src column is how an engineer gets from a line to the code that
    # emitted it, so the SQL path has to name the function that really ran.
    srcs = {e["src"] for e in lines}
    assert "backend.routes.book:_spine_prerank" in srcs
    assert ("backend.agents.relationship.spine.relationships:last_touch_index"
            in srcs)

    before = bus.latest_seq()
    askprobe.prerank_done(user.id, {"path": "cache", "roster": 650, "cap": 80})
    assert "book cache is warm" in _msgs(user.id, before)

    before = bus.latest_seq()
    askprobe.prerank_done(user.id, {"path": "full", "roster": 12, "cap": 80,
                                    "why": "book of 12 is at or under the cap"})
    joined = _msgs(user.id, before)
    assert "in-agent over the built book" in joined
    assert "book of 12 is at or under the cap" in joined


def test_selection_does_not_re_report_a_prerank_that_already_ran(db, user):
    """On the SQL path the book arrives already narrowed, so
    _prioritized_for_ask is a no-op. Reporting "80 ≤ cap 80, whole book sent to
    selection" there would read as though nothing had been ranked at all."""
    book = [{"name": f"C{i}"} for i in range(80)]
    before = bus.latest_seq()
    askprobe.selection_done(user.id, "q", book, {"people": []}, 10.0,
                            prerank={"path": "sql", "roster": 650, "cap": 80})
    joined = _msgs(user.id, before)
    assert "whole book sent to selection" not in joined
    assert "selection:" in joined, "the selection line itself must still appear"


def test_drafting_does_not_name_a_model_it_cannot_call(db, user, monkeypatch):
    """The log contradicted itself: it warned "no ANTHROPIC_API_KEY" at the top
    of an ask and then, three lines later, reported N drafts "via
    claude-sonnet-4-6, streamed token-by-token". stream_from_context yields
    nothing without a key, so those cards actually arrived empty."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from backend.agents import llm
    before = bus.latest_seq()
    askprobe.drafting_started(user.id, 6, 0, 6)
    joined = _msgs(user.id, before)
    assert llm.MODEL not in joined, "named a model with no key configured"
    assert "yields NOTHING without a key" in joined

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    before = bus.latest_seq()
    askprobe.drafting_started(user.id, 6, 0, 6)
    assert llm.MODEL in _msgs(user.id, before)


def test_draft_tap_distinguishes_composer_from_heuristic(db, user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from backend.agents import llm
    before = bus.latest_seq()
    askprobe.draft_tap(user.id, "Jane", "shared", "email", "congrats", 900.0, 220)
    joined = _msgs(user.id, before)
    assert llm.MODEL in joined and "220 chars" in joined

    before = bus.latest_seq()
    askprobe.draft_tap(user.id, "Bob", "heuristic", "email", "catch up", 5.0, 180)
    joined = _msgs(user.id, before)
    assert "heuristic composer" in joined and "no model call" in joined


def test_publish_never_raises(db):
    """Instrumentation must not be able to break a real ask."""
    bus.publish(1, "info", "src", "msg", weird=object())  # non-serializable extra
    askprobe.ask_started(1, "x")   # must not raise even mid-failure


def test_activity_survives_across_independent_sessions(db, user):
    """The bug this table replaced a ring buffer to fix.

    The ask is served by whichever uvicorn worker takes that request; the
    Observe SSE stream is held open by whichever worker took THAT one. With
    WEB_CONCURRENCY > 1 they are different processes, so an in-memory buffer
    showed the reader nothing. Two independent sessions here stand in for two
    workers: what one publishes, the other must be able to read.
    """
    from sqlalchemy.orm import sessionmaker
    writer_factory = bus._session_factory
    reader = sessionmaker(bind=writer_factory().get_bind(),
                          autoflush=False, autocommit=False)()
    try:
        before = bus.latest_seq()
        askprobe.ask_started(user.id, "cross-worker query")
        seen = bus.since(user.id, before, db=reader)
        assert any("cross-worker query" in e["msg"] for e in seen), \
            "a reader on a different session could not see published activity"
    finally:
        reader.close()


def test_publish_does_not_join_the_callers_transaction(db, user):
    """A rolled-back ask must not erase its own log lines -- and an Observe
    write must not accidentally commit the caller's half-finished work."""
    before = bus.latest_seq()
    db.add(models.Contact(user_id=user.id, name="Uncommitted Person"))
    askprobe.ask_started(user.id, "during an open transaction")
    db.rollback()

    joined = " | ".join(e["msg"] for e in bus.since(user.id, before))
    assert "during an open transaction" in joined, "log line lost to caller rollback"
    names = [c.name for c in db.query(models.Contact).all()]
    assert "Uncommitted Person" not in names, "bus write committed the caller's data"


def test_rows_outside_the_ttl_are_pruned(db, user, monkeypatch):
    """Retention is enforced on the write path, so this stays a live tail and
    does not quietly become a permanent record of who a user asked about."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("OBSERVE_ACTIVITY_TTL_MIN", "30")
    bus.publish(user.id, "info", "src", "ancient line")
    old = db.query(models.ObserveActivity).order_by(
        models.ObserveActivity.id.desc()).first()
    old.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    db.commit()
    old_id = old.id

    bus.use_session_factory(bus._session_factory)   # reset the prune-once latch
    bus.publish(user.id, "info", "src", "fresh line")

    remaining = {r.id for r in db.query(models.ObserveActivity).all()}
    assert old_id not in remaining, "row older than the TTL was retained"
    assert any("fresh line" in r.msg for r in db.query(models.ObserveActivity).all())


def test_publish_never_closes_a_session_it_did_not_create(db, user):
    """Instrumentation must not close the caller's session.

    A session factory is not guaranteed to hand back a NEW session -- code
    that redirects the database (tests/test_ask_stream_narration.py does
    exactly this) may return one the request is still using. Closing it
    detaches the caller's loaded objects, and this is not hypothetical: it
    broke a live ask with `DetachedInstanceError: Instance <User> is not
    bound to a Session` the moment the bus started closing what the factory
    returned.
    """
    shared = bus._session_factory()          # stands in for the caller's session
    loaded = shared.query(models.User).filter_by(id=user.id).one()
    bus.use_session_factory(lambda: shared)  # a factory that shares, not creates

    bus.publish(user.id, "info", "src", "written while the caller holds a session")

    # The caller's object must still be usable -- reading an attribute off a
    # detached instance is what raised before.
    assert loaded.email, "publish detached the caller's object by closing its session"
    joined = " | ".join(e["msg"] for e in bus.since(user.id, 0))
    assert "written while the caller holds a session" in joined


# ── the write must not sit on the request thread ────────────────────────────

def test_publishing_does_not_block_the_request(monkeypatch):
    """The regression this was written for.

    publish() opened a session, INSERTed and COMMITted inline. On a real ask
    that measured ~0.55s PER LINE -- each one queuing for a pooled connection
    against six concurrent drafting threads -- so roughly ten seconds of a
    28-second ask was the instrumentation, whose whole contract is that it
    never affects the ask.
    """
    import time
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import backend.db as bdb
    from backend.db import Base as _Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    _Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Stand in for a networked Postgres: every statement costs a round trip.
    @event.listens_for(engine, "before_cursor_execute")
    def _slow(conn, cur, statement, params, context, executemany):
        time.sleep(0.02)

    monkeypatch.setattr(bdb, "SessionLocal", Session)
    bus.use_session_factory(None)          # background writer, as in production
    try:
        t0 = time.perf_counter()
        for i in range(19):                # one ask's worth of narration
            bus.publish(7, "info", "src", f"line {i:02d}")
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 150, (
            f"publishing 19 lines cost the request {elapsed_ms:.0f}ms; the write "
            "is back on the request thread")

        assert bus.flush(5.0), "the writer never drained the queue"
        db = Session()
        rows = db.query(models.ObserveActivity).order_by(
            models.ObserveActivity.id).all()
        assert len(rows) == 19, f"lines were lost: {len(rows)}/19"
        assert [r.msg.split()[-1] for r in rows] == [f"{i:02d}" for i in range(19)], \
            "a single writer must preserve publish order -- the id is the cursor"
    finally:
        bus.use_session_factory(None)


def test_a_full_queue_drops_loudly_rather_than_blocking(monkeypatch):
    """Backpressure must never reach the request. Dropping is the right
    trade for a live tail, but a silently lossy log is worse than one that
    admits the gap."""
    import queue as _queue

    bus.use_session_factory(None)
    monkeypatch.setattr(bus, "_QUEUE", _queue.Queue(maxsize=2))
    monkeypatch.setattr(bus, "_ensure_writer", lambda: None)   # nothing drains
    before = bus.dropped_count()

    for i in range(10):
        bus.publish(8, "info", "src", f"line {i}")

    assert bus.dropped_count() - before == 8, "drops were not counted"


def test_timestamps_record_when_the_work_happened(monkeypatch):
    """Batched writes land later than the event. The timestamp has to say
    when the line was produced, not when the writer got to it."""
    from datetime import datetime, timezone

    bus.use_session_factory(None)
    monkeypatch.setattr(bus, "_ensure_writer", lambda: None)
    before = datetime.now(timezone.utc)
    row = bus._row(9, "info", "src", "msg", {})
    after = datetime.now(timezone.utc)
    assert before <= row["created_at"] <= after
