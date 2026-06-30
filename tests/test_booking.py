"""Tests for the booking action (integrations/booking + the client create methods).

No live Google/Graph: httpx is mocked at the client `_post` seam, and the token getter
is monkeypatched. Covers: client request shapes (Meet/Teams video, sendUpdates, the
notify=False slot-hold for Graph), provider auto-pick (google > microsoft) + override,
end-time math, and the orchestrator's clear ValueError reasons (no calendar / bad time /
upstream error).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import booking, google_client, outlook_client, oauth


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
    """An active ConnectedAccount with a far-future token (so no refresh path)."""
    return oauth.save_tokens(
        db, user_id=user_id, provider=provider, account_email=f"{provider}@x.com",
        tokens={"access_token": f"{provider}-tok", "refresh_token": "r",
                "expires_in": 3600})


# ── client request shapes ─────────────────────────────────────────────────────
def test_google_create_event_sends_meet_and_invites(monkeypatch):
    seen = {}
    def fake_post(token, url, body, params=None):
        seen["token"], seen["url"], seen["body"], seen["params"] = token, url, body, params
        return {"id": "ev1", "htmlLink": "https://cal/ev1",
                "hangoutLink": "https://meet.google.com/abc",
                "start": {"dateTime": "2026-07-01T15:00:00-07:00"},
                "attendees": [{"email": "sarah@x.com"}]}
    monkeypatch.setattr(google_client, "_post", fake_post)
    out = google_client.create_calendar_event(
        "tok", summary="Coffee", start_iso="2026-07-01T15:00:00-07:00",
        end_iso="2026-07-01T15:30:00-07:00", attendees=["sarah@x.com"])
    assert out["id"] == "ev1" and out["video_url"] == "https://meet.google.com/abc"
    assert out["html_link"] == "https://cal/ev1"
    # invite emailed + Meet requested
    assert seen["params"]["sendUpdates"] == "all"
    assert seen["params"]["conferenceDataVersion"] == 1
    assert seen["body"]["conferenceData"]["createRequest"]["conferenceSolutionKey"][
        "type"] == "hangoutsMeet"
    assert seen["body"]["attendees"] == [{"email": "sarah@x.com"}]


def test_google_create_event_no_notify_no_video(monkeypatch):
    monkeypatch.setattr(google_client, "_post",
                        lambda t, u, b, params=None: {"id": "e", "start": {}, "attendees": []})
    captured = {}
    def fake_post(token, url, body, params=None):
        captured["body"], captured["params"] = body, params
        return {"id": "e", "start": {}, "attendees": []}
    monkeypatch.setattr(google_client, "_post", fake_post)
    google_client.create_calendar_event(
        "tok", summary="Hold", start_iso="2026-07-01T15:00:00Z",
        end_iso="2026-07-01T15:30:00Z", attendees=["s@x.com"],
        add_video=False, notify=False)
    assert captured["params"]["sendUpdates"] == "none"
    assert "conferenceData" not in captured["body"]
    assert "conferenceDataVersion" not in captured["params"]


def test_google_meet_url_from_entry_points(monkeypatch):
    monkeypatch.setattr(google_client, "_post", lambda *a, **k: {
        "id": "e", "start": {}, "attendees": [], "conferenceData": {"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1"},
            {"entryPointType": "video", "uri": "https://meet.google.com/xyz"}]}})
    out = google_client.create_calendar_event(
        "t", summary="x", start_iso="2026-07-01T15:00:00Z",
        end_iso="2026-07-01T15:30:00Z", attendees=[])
    assert out["video_url"] == "https://meet.google.com/xyz"


def test_outlook_create_event_teams_and_invites(monkeypatch):
    captured = {}
    def fake_post(token, url, body):
        captured["url"], captured["body"] = url, body
        return {"id": "o1", "webLink": "https://outlook/o1",
                "onlineMeeting": {"joinUrl": "https://teams/join"},
                "start": {"dateTime": "2026-07-01T15:00:00"},
                "attendees": [{"emailAddress": {"address": "sarah@x.com"}}]}
    monkeypatch.setattr(outlook_client, "_post", fake_post)
    out = outlook_client.create_calendar_event(
        "tok", summary="Coffee", start_iso="2026-07-01T15:00:00",
        end_iso="2026-07-01T15:30:00", attendees=["sarah@x.com"])
    assert out["video_url"] == "https://teams/join" and out["html_link"] == "https://outlook/o1"
    assert captured["body"]["isOnlineMeeting"] is True
    assert captured["body"]["attendees"][0]["emailAddress"]["address"] == "sarah@x.com"


def test_outlook_no_notify_holds_slot_without_attendees(monkeypatch):
    captured = {}
    def fake_post(token, url, body):
        captured["body"] = body
        return {"id": "o", "start": {}, "attendees": []}
    monkeypatch.setattr(outlook_client, "_post", fake_post)
    outlook_client.create_calendar_event(
        "tok", summary="Hold", start_iso="2026-07-01T15:00:00",
        end_iso="2026-07-01T15:30:00", attendees=["s@x.com"], notify=False)
    assert "attendees" not in captured["body"]   # slot held, no invite sent


# ── orchestrator: provider pick + error reasons ───────────────────────────────
def test_book_meeting_prefers_google_then_microsoft(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "microsoft")
    _connect(db, u.id, "google")
    used = {}
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    def fake_google(*a, **k):
        used["p"] = "google"
        return {"id": "g"}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_google)
    out = booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                               start_iso="2026-07-01T15:00:00Z")
    assert out["provider"] == "google" and used["p"] == "google"


def test_book_meeting_provider_override(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")
    _connect(db, u.id, "microsoft")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(outlook_client, "create_calendar_event", lambda *a, **k: {"id": "o"})
    out = booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                               start_iso="2026-07-01T15:00:00Z", provider="microsoft")
    assert out["provider"] == "microsoft"


def test_book_meeting_no_calendar_connected(db):
    u = _user(db)
    with pytest.raises(ValueError, match="no calendar connected"):
        booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                             start_iso="2026-07-01T15:00:00Z")


def test_book_meeting_bad_time(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    with pytest.raises(ValueError, match="invalid start time"):
        booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                             start_iso="next tuesday")


def test_book_meeting_upstream_error_is_clean(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    def boom(*a, **k):
        raise RuntimeError("403 forbidden")
    monkeypatch.setattr(google_client, "create_calendar_event", boom)
    with pytest.raises(ValueError, match="google calendar error"):
        booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                             start_iso="2026-07-01T15:00:00Z")


def test_book_meeting_token_refresh_failure(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: None)
    with pytest.raises(ValueError, match="needs reconnection"):
        booking.book_meeting(db, u, attendee_email="s@x.com", title="x",
                             start_iso="2026-07-01T15:00:00Z")


def test_end_iso_adds_duration():
    assert booking._end_iso("2026-07-01T15:00:00+00:00", 30) == "2026-07-01T15:30:00+00:00"
    assert booking._end_iso("2026-07-01T15:00:00-07:00", 45) == "2026-07-01T15:45:00-07:00"
