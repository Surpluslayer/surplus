"""tests/test_ask_stream_narration.py : confirms the ask-bar chips (e.g.
"Who's gone quiet lately") narrate into the Observe activity log in real
time. Unlike draft_stream (see test_draft_stream_narration.py, fixed
separately), ask_stream's askprobe wiring (ask_started/book_loaded/
selection_done/drafting_started/ask_done) was already present -- this
test exists to CONFIRM that end-to-end against a real request/response
cycle, not just read the code, the same way test_draft_stream_narration.py
caught a real contact-resolution bug that a code read alone missed.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend.auth import current_user
from backend.demo import cohort
from backend.main import app
from backend import models
from backend.observe import bus


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _override_db

    # ask_stream's work() thread, like draft_stream's generator, opens its
    # OWN session via backend.db.SessionLocal (the request-scoped session
    # closes before a background thread/stream finishes) -- dependency_
    # overrides can't reach that. See test_draft_stream_narration.py for
    # why an unpatched SessionLocal silently resolves against the wrong
    # (real default) database instead of this test's isolated one.
    seed_db = Session()
    monkeypatch.setattr("backend.db.SessionLocal", lambda: seed_db)
    # bus writes are batched onto a background thread in production, so a test
    # that reads the log the instant the request returns would race the writer.
    # The injected factory puts the bus in its deterministic synchronous mode
    # (see bus.publish) -- same seam tests/test_observe_askprobe.py uses.
    from backend.observe import bus as _bus
    _bus.use_session_factory(lambda: seed_db)

    cohort.generate(seed_db, n_lawyers=3, days=30, cohort_id="ask-stream-narration")
    user = seed_db.execute(select(models.User)).scalars().first()
    user_id = user.id

    def _current():
        s = Session()
        try:
            return s.get(models.User, user_id)
        finally:
            s.expunge_all()
            s.close()
    app.dependency_overrides[current_user] = _current

    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c, user_id
    app.dependency_overrides.clear()
    _bus.use_session_factory(None)


def test_who_has_gone_quiet_chip_narrates_the_full_lifecycle(client, monkeypatch):
    """The exact chip text from frontend/BookApp.jsx's CHIPS_BOOK. No
    ANTHROPIC_API_KEY -> deterministic keyword routing + heuristic drafts,
    the common case for this test environment -- confirms the narration
    still fires end-to-end on the no-key fallback path, not just the
    happy path with a real model."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c, user_id = client
    before = bus.latest_seq()

    r = c.post("/api/book/ask/stream", json={"query": "Who's gone quiet lately", "mode": "book"})
    assert r.status_code == 200

    events = bus.since(user_id, before)
    joined = " | ".join(e["msg"] for e in events)

    # ask_started
    assert "ask bar: \"Who's gone quiet lately\"" in joined
    assert "no ANTHROPIC_API_KEY" in joined
    # book_loaded
    assert "book loaded:" in joined
    # selection_done
    assert "selection:" in joined
    # drafting_started (only fires if there's at least one target -- assert
    # the marker line always present regardless)
    # ask_done
    assert "ask complete:" in joined

    # Real ordering: a reader tailing the log sees the SAME order the request
    # actually executed in, not narration collected out of sequence.
    kinds = [e["msg"] for e in events]
    assert any("ask bar:" in m for m in kinds)
    idx_start = next(i for i, m in enumerate(kinds) if "ask bar:" in m)
    idx_done = next(i for i, m in enumerate(kinds) if "ask complete:" in m)
    assert idx_start < idx_done


def test_a_big_book_takes_the_sql_prerank_end_to_end(client, monkeypatch):
    """The SQL pre-rank through a real request, not just its unit tests.

    tests/test_book_prerank_sql.py proves the ranking is the same ranking; this
    proves the ask actually reaches it -- that a >cap book routes through
    _book_for_ask's SQL path, still answers, and says so in the log with the
    numbers a reader can check against each other (roster > cap = rows never
    built)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    c, user_id = client

    from backend.db import SessionLocal
    from backend.routes import book as routes_book
    from datetime import datetime, timedelta, timezone
    s = SessionLocal()
    # The cohort seeds demo-lawyer-NNN@example.com, and a demo user's book is
    # the spine PLUS the demo roster -- a shape the pre-rank deliberately
    # refuses. Move this user off the demo convention so the request exercises
    # the real-account path the pre-rank is FOR.
    u = s.get(models.User, user_id)
    u.email, u.is_demo = "real-account@surpluslayer.com", False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(200):
        ct = models.Contact(user_id=user_id, name=f"Bulk {i:03d}",
                            primary_identity_key=f"{user_id}:bulk:{i}")
        s.add(ct)
        s.flush()
        s.add(models.RelationshipInteraction(
            actor_user_id=user_id, contact_id=ct.id,
            occurred_at=now - timedelta(days=(i % 120)),
            source_type="message", interaction_type="message", title="Message"))
    s.commit()
    routes_book._BOOK_CACHE.clear()

    before = bus.latest_seq()
    r = c.post("/api/book/ask/stream",
               json={"query": "Who's gone quiet lately", "mode": "book"})
    assert r.status_code == 200
    assert "event: people" in r.text, "the ask stopped answering on the SQL path"

    events = bus.since(user_id, before)
    joined = " | ".join(e["msg"] for e in events)
    assert "pre-rank (SQL):" in joined, f"the ask did not take the SQL path: {joined}"
    assert "→ top 80" in joined
    assert "built 80 book rows, not " in joined
    # The old in-agent line must not ALSO appear -- two pre-rank lines for one
    # pre-rank is exactly the split, contradictory log this work is undoing.
    assert "whole book sent to selection" not in joined
    assert joined.count("pre-rank") == 1, (
        "one pre-rank ran, so the log must carry exactly one pre-rank line: "
        + " | ".join(m for m in [e["msg"] for e in events] if "pre-rank" in m))
    assert "ranked from a last-touch index" in joined, "no provenance for the ranking"


def test_ask_stream_narration_survives_a_broken_askprobe_call(client, monkeypatch):
    """Matches draft_stream's posture: instrumentation must never turn a
    real ask into a dead stream. bus.publish already swallows its own
    errors (askprobe.py's own docstring); this pins that ask_stream's
    request still completes cleanly even if one askprobe call is broken."""
    from backend.observe import askprobe

    def _boom(*a, **kw):
        raise RuntimeError("narration exploded")
    monkeypatch.setattr(askprobe, "book_loaded", _boom)

    c, _user_id = client
    r = c.post("/api/book/ask/stream", json={"query": "Who's gone quiet lately", "mode": "book"})
    assert r.status_code == 200
    assert "event: done" in r.text or "event: people" in r.text
