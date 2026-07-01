"""Tests for the autonomous-booking building blocks that the draft+send pipeline
uses: calendar availability (find_open_slot), the create-event-on-send action
(agent_book_meeting: idempotent + email-required), the draft-time decision
(propose_meeting_slot: Calendly vs proposed time), and the send-side bridge
(fire_booking_on_send: fires for propose_time, no-ops for calendly).

No live Google/Graph/Calendly: the calendar read/write seams and the token getter
are monkeypatched, in-memory SQLite for the spine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import booking, google_client, oauth


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _user(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    return u


def _connect(db, user_id, provider, *, status="active"):
    return oauth.save_tokens(
        db, user_id=user_id, provider=provider, account_email=f"{provider}@x.com",
        tokens={"access_token": f"{provider}-tok", "refresh_token": "r",
                "expires_in": 3600})


def _contact(db, user, *, email=None, name="Sarah"):
    c = models.Contact(user_id=user.id, primary_identity_key="li:sarah",
                       name=name, email=email)
    db.add(c); db.commit()
    return c


# ── find_open_slot ────────────────────────────────────────────────────────────
def test_find_open_slot_returns_business_hours_iso(db, monkeypatch):
    u = _user(db); acct = _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    # Empty calendar -> a slot inside business hours, with a tz offset.
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [])
    slot = booking.find_open_slot(db, acct, duration_min=30,
                                  tz="America/Los_Angeles")
    assert slot is not None
    dt = datetime.fromisoformat(slot)
    assert dt.tzinfo is not None                      # offset-aware
    assert booking._BUSINESS_START_HOUR <= dt.hour < booking._BUSINESS_END_HOUR
    assert dt.weekday() < 5                            # a weekday


def test_find_open_slot_skips_busy_block(db, monkeypatch):
    u = _user(db); acct = _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")

    # Busy for the FIRST candidate of every day at 9am local; the slot returned
    # must not collide with that block.
    def busy(*a, **k):
        out = []
        base = datetime.now(timezone.utc)
        for d in range(0, 8):
            day = (base + timedelta(days=d))
            out.append({"start": day.replace(hour=16, minute=0, second=0,
                                             microsecond=0).isoformat(),
                        "summary": "blocked"})
        return out
    monkeypatch.setattr(google_client, "fetch_calendar_events", busy)
    slot = booking.find_open_slot(db, acct, duration_min=30,
                                  tz="America/Los_Angeles")
    assert slot is not None                            # there's still open time


# ── propose_meeting_slot ────────────────────────────────────────────────────────
def test_propose_meeting_slot_prefers_calendly(db, monkeypatch):
    u = _user(db); _connect(db, u.id, "calendly")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr("backend.integrations.calendly_client.scheduling_url",
                        lambda t: "https://calendly.com/host/30min")
    out = booking.propose_meeting_slot(db, u, duration_min=30)
    assert out["mode"] == "calendly"
    assert out["scheduling_url"] == "https://calendly.com/host/30min"


def test_propose_meeting_slot_falls_back_to_time(db, monkeypatch):
    u = _user(db); _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [])
    out = booking.propose_meeting_slot(db, u, duration_min=30)
    assert out["mode"] == "propose_time"
    assert out["start_iso"]
    assert out["with_zoom"] is False                   # no zoom connected


def test_propose_meeting_slot_none_when_nothing_connected(db):
    u = _user(db)
    out = booking.propose_meeting_slot(db, u)
    assert out["mode"] == "none"


# ── agent_book_meeting: create + invite + record ───────────────────────────────
def test_agent_book_meeting_creates_event_and_records(db, monkeypatch):
    u = _user(db); _connect(db, u.id, "google")
    c = _contact(db, u, email="sarah@x.com")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [])
    seen = {}
    def fake_create(token, **kw):
        seen.update(kw)
        return {"id": "ev9", "html_link": "https://cal/ev9",
                "video_url": "https://meet.google.com/abc",
                "start": kw["start_iso"], "attendees": [kw["attendees"][0]]}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_create)

    out = booking.agent_book_meeting(db, u, c, topic="Quick chat", duration_min=30)
    assert out["already_booked"] is False
    assert out["id"] == "ev9"
    assert seen["attendees"] == ["sarah@x.com"]        # contact invited
    # A meeting_booked interaction is recorded on the spine.
    ri = (db.query(models.RelationshipInteraction)
          .filter_by(actor_user_id=u.id, contact_id=c.id,
                     source_type="meeting_booked").one())
    assert "sarah@x.com" in (ri.meta_json or "")


def test_agent_book_meeting_is_idempotent(db, monkeypatch):
    u = _user(db); _connect(db, u.id, "google")
    c = _contact(db, u, email="sarah@x.com")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [])
    calls = {"n": 0}
    def fake_create(token, **kw):
        calls["n"] += 1
        return {"id": f"ev{calls['n']}", "html_link": "h",
                "video_url": None, "start": kw["start_iso"],
                "attendees": [kw["attendees"][0]]}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_create)

    first = booking.agent_book_meeting(db, u, c, topic="Chat")
    second = booking.agent_book_meeting(db, u, c, topic="Chat")
    assert calls["n"] == 1                              # only ONE event created
    assert second["already_booked"] is True
    assert second.get("event_id") == first["id"]


def test_agent_book_meeting_requires_email(db, monkeypatch):
    u = _user(db); _connect(db, u.id, "google")
    c = _contact(db, u, email=None)                     # no email on file
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    with pytest.raises(ValueError, match="no email"):
        booking.agent_book_meeting(db, u, c, topic="Chat")


def test_contact_email_falls_back_to_identity(db):
    u = _user(db)
    c = _contact(db, u, email=None)
    db.add(models.ContactIdentity(contact_id=c.id, user_id=u.id, kind="email",
                                  value="sarah.alt@x.com", is_primary=True))
    db.commit()
    assert booking.contact_email(db, c) == "sarah.alt@x.com"


# ── fire_booking_on_send: the send-side bridge ─────────────────────────────────
def test_fire_booking_on_send_calendly_is_noop(db):
    from backend.agents.relationship.pipeline.send.sender import fire_booking_on_send
    u = _user(db); c = _contact(db, u, email="s@x.com")
    out = fire_booking_on_send(db, u, c, {"mode": "calendly",
                                          "scheduling_url": "https://c/x"})
    assert out["booked"] is False and out["mode"] == "calendly"


def test_fire_booking_on_send_propose_time_books(db, monkeypatch):
    from backend.agents.relationship.pipeline.send.sender import fire_booking_on_send
    u = _user(db); _connect(db, u.id, "google")
    c = _contact(db, u, email="s@x.com")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(google_client, "fetch_calendar_events", lambda *a, **k: [])
    monkeypatch.setattr(google_client, "create_calendar_event",
                        lambda token, **kw: {"id": "ev1", "html_link": "h",
                                             "video_url": "v", "start": kw["start_iso"],
                                             "attendees": [kw["attendees"][0]]})
    payload = {"mode": "propose_time", "start_iso": "2099-07-01T17:00:00+00:00",
               "duration_min": 30, "tz": "UTC", "with_zoom": False}
    out = fire_booking_on_send(db, u, c, payload, topic="Chat")
    assert out["booked"] is True and out["id"] == "ev1"


def test_fire_booking_on_send_missing_email_does_not_raise(db, monkeypatch):
    from backend.agents.relationship.pipeline.send.sender import fire_booking_on_send
    u = _user(db); _connect(db, u.id, "google")
    c = _contact(db, u, email=None)
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    payload = {"mode": "propose_time", "start_iso": "2099-07-01T17:00:00+00:00",
               "duration_min": 30, "tz": "UTC"}
    out = fire_booking_on_send(db, u, c, payload)
    assert out["booked"] is False and "email" in out["reason"]
