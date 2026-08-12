"""tests/test_draft_stream_narration.py : POST /api/book/draft/stream is
DraftSheet's PRIMARY path (bookDraftStream is tried first in
frontend/BookApp.jsx; bookDraft only runs as a fallback when the SSE
connection fails to open) -- but only the non-streaming /draft handler
called askprobe.draft_tap. A real Draft tap almost never reached the
Observe activity log; only the rare fallback did. This verifies the
streaming path narrates too, for both the shared-composer and
heuristic-fallback engines.
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

    # draft_stream's background generator does NOT use the request-scoped
    # `db` param -- like every other SSE route in this codebase (routes/
    # book.py's ask_stream, routes/observe.py's streams), it opens its own
    # session via `from ..db import SessionLocal; wdb = SessionLocal()`
    # because the request-scoped session closes before a streaming body is
    # fully consumed. dependency_overrides can't reach that -- it has to be
    # patched directly (same convention test_catch_up_ingest.py already
    # uses for the same reason). Without this, `wdb` binds to whatever
    # backend.db.SessionLocal's real default engine is, and a contact_id
    # that happens to collide with a row in THAT database silently narrates
    # the wrong person.
    seed_db = Session()
    monkeypatch.setattr("backend.db.SessionLocal", lambda: seed_db)
    # bus writes are batched onto a background thread in production, so a test
    # that reads the log the instant the request returns would race the writer.
    # The injected factory puts the bus in its deterministic synchronous mode
    # (see bus.publish) -- same seam tests/test_observe_askprobe.py uses.
    from backend.observe import bus as _bus
    _bus.use_session_factory(lambda: seed_db)

    cohort.generate(seed_db, n_lawyers=2, days=14, cohort_id="draft-stream-narration")
    user = seed_db.execute(select(models.User)).scalars().first()
    contact = seed_db.execute(select(models.Contact)
                              .where(models.Contact.user_id == user.id)).scalars().first()
    user_id, contact_id, contact_name = user.id, contact.id, contact.name

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
        yield c, user_id, contact_id, contact_name
    app.dependency_overrides.clear()
    _bus.use_session_factory(None)


def test_draft_stream_narrates_heuristic_fallback(client, monkeypatch):
    """No ANTHROPIC_API_KEY -> compose_stream yields nothing -> the heuristic
    branch runs. draft_tap must still fire, with engine='heuristic'."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c, user_id, contact_id, contact_name = client
    before = bus.latest_seq()

    r = c.post("/api/book/draft/stream",
              json={"contact_id": str(contact_id), "trigger": "catch up",
                    "channel": "email"})
    assert r.status_code == 200

    events = bus.since(user_id, before)
    joined = " | ".join(e["msg"] for e in events)
    assert "draft tap" in joined
    assert contact_name in joined
    assert "heuristic composer" in joined
    assert "no model call" in joined


def test_draft_stream_narrates_shared_composer(client, monkeypatch):
    """A real Contact + a key present -> compose_stream is used. draft_tap
    must fire with engine='shared' and name the real drafting model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from backend.agents.relationship.pipeline.compose import drafting
    from backend.agents import llm

    def _fake_compose_stream(db, user_id, contact, *, reason, channel):
        yield "Hi "
        yield "there, congrats!"
    monkeypatch.setattr(drafting, "compose_stream", _fake_compose_stream)

    c, user_id, contact_id, contact_name = client
    before = bus.latest_seq()

    r = c.post("/api/book/draft/stream",
              json={"contact_id": str(contact_id), "trigger": "promotion",
                    "channel": "linkedin"})
    assert r.status_code == 200

    events = bus.since(user_id, before)
    joined = " | ".join(e["msg"] for e in events)
    assert "draft tap" in joined
    assert contact_name in joined
    assert llm.MODEL in joined
    # "Hi " + "there, congrats!" is 19 chars -- confirms real streamed
    # length (summed across chunks), not a hardcoded/zero placeholder.
    assert "19 chars" in joined


def test_draft_stream_never_raises_when_narration_breaks(client, monkeypatch):
    """askprobe itself is best-effort, but the call site is independently
    wrapped too (matching /draft's own posture) -- a narration bug must
    never turn a real draft into a 500."""
    from backend.observe import askprobe

    def _boom(*a, **kw):
        raise RuntimeError("narration exploded")
    monkeypatch.setattr(askprobe, "draft_tap", _boom)

    c, _user_id, contact_id, _name = client
    r = c.post("/api/book/draft/stream",
              json={"contact_id": str(contact_id), "trigger": "catch up",
                    "channel": "email"})
    assert r.status_code == 200
    assert "event: done" in r.text
    assert "event: error" not in r.text
