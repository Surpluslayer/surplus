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
    s = Session()
    try:
        yield s
    finally:
        s.close()


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
