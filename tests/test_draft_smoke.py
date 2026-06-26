"""Guard test for the live draft-pipeline smoke driver (scripts/draft_smoke.py).

The driver itself needs a real API key (it runs live Sonnet), so it's not run in
CI. This pins the SEED fixture so it can't silently bit-rot — the whole point of
the smoke is that the seeded threads carry the signals the real agent triages on
(an owed-deck open loop for Sarah; a just-replied "no rush" for Tom; a 62-day
quiet single-opener for Mia). If the timeline plumbing changes shape, this fails
loudly instead of the live smoke quietly testing nothing.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models import Base
from backend import models
from backend.agents.relationship import relationships as rel
from backend.agents.relationship import relationship_agent as ragent

from scripts import draft_smoke


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _thread_for(db, name):
    contact = next(c for c in rel.list_contacts(db, 1)
                   if rel.contact_summary(db, c).get("name") == name)
    return ragent._thread_from_timeline(rel.contact_timeline(db, contact))


def test_seed_creates_three_contacts(db):
    draft_smoke._seed(db)
    names = {rel.contact_summary(db, c).get("name")
             for c in rel.list_contacts(db, 1)}
    assert names == {"Sarah Lin", "Tom Reed", "Mia Park"}


def _convo(thread):
    """Just the spoken messages — drop the leading capture 'context' row."""
    return [m for m in thread if m["who"] in ("them", "host")]


def test_sarah_thread_is_an_owed_promise_open_loop(db):
    draft_smoke._seed(db)
    thread = _thread_for(db, "Sarah Lin")
    # them asked for the deck, then the host promised to send it (host spoke last).
    convo = _convo(thread)
    assert [m["who"] for m in convo] == ["them", "host"]
    assert "deck" in convo[0]["text"].lower()
    assert "send it over" in convo[-1]["text"].lower()
    # the real signal layer must see an open host promise here
    sig = ragent._thread_signals(thread)
    assert sig.get("host_open_promise") is True


def test_tom_thread_has_ball_in_his_court(db):
    draft_smoke._seed(db)
    thread = _thread_for(db, "Tom Reed")
    # host spoke last ("no rush"), so the contact is NOT awaiting the host.
    assert thread[-1]["who"] == "host"
    assert ragent._thread_signals(thread).get("awaiting_host_reply") is not True


def test_mia_thread_is_a_lone_quiet_opener(db):
    draft_smoke._seed(db)
    convo = _convo(_thread_for(db, "Mia Park"))
    assert len(convo) == 1 and convo[0]["who"] == "host"


def test_voice_examples_are_channel_tagged(db):
    """The seed deliberately mixes a formal EMAIL example in so the live smoke
    proves Step 4 scoping filters it out of a linkedin follow-up."""
    draft_smoke._seed(db)
    from backend.agents import voice
    user = db.get(models.User, 1)
    # scoped to linkedin → only the two casual linkedin examples survive
    li = voice.resolve_voice_examples_for_user(user, channel="linkedin",
                                               message_type="warm_followup")
    assert all("Dear Mr. Patel" not in e for e in li)
    assert any("grab coffee" in e for e in li)
