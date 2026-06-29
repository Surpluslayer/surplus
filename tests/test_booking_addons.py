"""Tests for the booking add-ons: Zoom create-meeting + Calendly scheduling link,
plus the Basic-auth token exchange Zoom needs.

No live Zoom/Calendly: httpx + the token getter are mocked.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.integrations import (booking, google_client, zoom_client,
                                  calendly_client, oauth, providers)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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


def _connect(db, user_id, provider):
    return oauth.save_tokens(db, user_id=user_id, provider=provider,
                             account_email=f"{provider}@x.com",
                             tokens={"access_token": f"{provider}-tok",
                                     "refresh_token": "r", "expires_in": 3600})


# ── zoom client ────────────────────────────────────────────────────────────────
def test_zoom_create_meeting(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"id": 123, "join_url": "https://zoom.us/j/123",
                                "start_url": "https://zoom.us/s/123"}
    monkeypatch.setattr(zoom_client.httpx, "post", lambda *a, **k: R())
    out = zoom_client.create_meeting("tok", topic="Chat", start_iso="2026-07-01T15:00:00Z")
    assert out["join_url"] == "https://zoom.us/j/123"


# ── booking with_zoom ─────────────────────────────────────────────────────────
def test_book_with_zoom_uses_zoom_link(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")
    _connect(db, u.id, "zoom")
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    monkeypatch.setattr(zoom_client, "create_meeting",
                        lambda *a, **k: {"join_url": "https://zoom.us/j/9"})
    captured = {}
    def fake_event(token, **k):
        captured.update(k)
        return {"id": "ev", "html_link": "h", "video_url": None, "start": "s", "attendees": []}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_event)
    out = booking.book_meeting(db, u, attendee_email="s@x.com", title="Chat",
                               start_iso="2026-07-01T15:00:00Z", with_zoom=True)
    assert out["video_url"] == "https://zoom.us/j/9"
    assert captured["add_video"] is False                 # native video suppressed
    assert "Zoom: https://zoom.us/j/9" in captured["description"]


def test_book_with_zoom_falls_back_when_zoom_absent(db, monkeypatch):
    u = _user(db)
    _connect(db, u.id, "google")            # no zoom connected
    monkeypatch.setattr(oauth, "get_valid_access_token", lambda d, a, **k: "tok")
    def fake_event(token, **k):
        return {"id": "ev", "html_link": "h", "video_url": "https://meet.google.com/x",
                "start": "s", "attendees": []}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_event)
    out = booking.book_meeting(db, u, attendee_email="s@x.com", title="Chat",
                               start_iso="2026-07-01T15:00:00Z", with_zoom=True)
    assert out["video_url"] == "https://meet.google.com/x"   # native Meet, Zoom skipped


# ── calendly scheduling link ──────────────────────────────────────────────────
def test_calendly_scheduling_url(monkeypatch):
    monkeypatch.setattr(calendly_client, "_get",
                        lambda *a, **k: {"resource": {"scheduling_url": "https://calendly.com/me"}})
    assert calendly_client.scheduling_url("tok") == "https://calendly.com/me"


# ── zoom OAuth uses Basic auth on the token endpoint ──────────────────────────
def test_zoom_token_exchange_uses_basic_auth(monkeypatch):
    monkeypatch.setenv("ZOOM_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "sec")
    seen = {}
    class R:
        def raise_for_status(self): pass
        def json(self): return {"access_token": "z"}
    def fake_post(url, data=None, headers=None, timeout=None):
        seen["headers"] = headers or {}
        seen["data"] = data or {}
        return R()
    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    oauth.exchange_code("zoom", code="c", redirect_uri="https://x/cb")
    assert seen["headers"].get("Authorization", "").startswith("Basic ")
    assert "client_secret" not in seen["data"]            # creds in the header, not body
