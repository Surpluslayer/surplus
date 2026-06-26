"""
Tests for the shared helpers extracted in the cleanup pass:

  backend/jsonx.py::extract_json
  backend/agents/sender.py::send_and_log
  backend/providers/__init__.py::get_provider_for_prospect
  backend/providers/unipile.py::fetch_thread (dry-run fixture)

These were previously only exercised indirectly via reply_agent / webhook
tests. Pinning them here so a future refactor breaks the unit test, not
the integration.
"""
from __future__ import annotations
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.relationship.sender import send_and_log
from backend.db import Base
from backend.jsonx import extract_json
from backend.providers import (
    UnipileProvider, get_provider, get_provider_for_prospect,
    reset_provider_cache,
)


# ── jsonx.extract_json ─────────────────────────────────────────────────

def test_extract_json_parses_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_markdown_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_strips_fence_without_lang():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_finds_object_in_prose():
    """Fallback: brace-search when the model leaks prose around the JSON."""
    assert extract_json('here you go: {"x": 2} hope this helps')["x"] == 2


def test_extract_json_returns_none_on_garbage():
    assert extract_json("just words no braces") is None


def test_extract_json_returns_none_on_empty():
    assert extract_json("") is None
    assert extract_json(None) is None  # robust to None input
    assert extract_json("   ") is None


def test_extract_json_returns_none_on_unmatched_braces():
    assert extract_json("{ not json at all") is None


# ── sender.send_and_log ────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    reset_provider_cache()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = TestingSession()
    try:
        yield s
    finally:
        s.close()
        reset_provider_cache()


def _seed(db):
    ev = models.Event(
        role="x", seniority="Staff+", co_stage="Seed", headcount=40,
        format="Sit-down dinner", city="SF", goal="Hiring pipeline",
        budget=8000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="m", name="Maya", role="Eng", company="X",
        seniority="Staff+", side="Builds", works_on="o", offers="", seeks="",
        li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya",
        linkedin_provider_id="li_maya", sources="linkedin", fit_score=88,
        status="contacted",
    )
    db.add(p); db.commit()
    return ev, p


def test_send_and_log_writes_success_state(db):
    _ev, p = _seed(db)
    res = send_and_log(
        db, p, "hello",
        sent_state="auto_reply_sent", fallback_provider=get_provider(),
    )
    assert res.dry_run is True
    db.expire_all()
    logs = db.get(models.Prospect, p.id).outreach
    assert len(logs) == 1
    assert logs[0].state == "dry_run_queued" or logs[0].state == "auto_reply_sent"
    # dry-run returns state=dry_run_queued; we record sent_state on success
    # : both are "not error" so the row is the sent_state we requested.
    assert logs[0].state == "auto_reply_sent"


def test_send_and_log_raises_when_event_missing(db):
    p = models.Prospect(
        event_id=99999, identity="x", name="x", role="x", company="x",
        status="contacted",
    )
    db.add(p); db.commit()
    p.event = None
    with pytest.raises(ValueError, match="has no event"):
        send_and_log(
            db, p, "hello",
            sent_state="message_sent", fallback_provider=get_provider(),
        )


def test_send_and_log_commit_false_defers(db):
    """commit=False lets the caller batch multiple sends in one transaction.
    The log row should still be added to the session, just not committed."""
    _ev, p = _seed(db)
    send_and_log(
        db, p, "hello",
        sent_state="follow_up_sent", fallback_provider=get_provider(),
        commit=False,
    )
    # Row is in session but unflushed : rollback should drop it
    db.rollback()
    db.expire_all()
    assert db.get(models.Prospect, p.id).outreach == []


def test_send_and_log_caps_body_at_8kb(db):
    """The OutreachLog body is capped at 8000 chars so a huge AI draft
    doesn't bloat the table."""
    _ev, p = _seed(db)
    huge = "x" * 20_000
    send_and_log(
        db, p, huge,
        sent_state="auto_reply_sent", fallback_provider=get_provider(),
    )
    db.expire_all()
    assert len(db.get(models.Prospect, p.id).outreach[0].body) == 8000


# ── providers.get_provider_for_prospect ────────────────────────────────

def test_routing_falls_back_when_no_event():
    fallback = object()
    p = SimpleNamespace(event=None)
    assert get_provider_for_prospect(p, fallback) is fallback


def test_routing_falls_back_when_no_user_id():
    fallback = object()
    p = SimpleNamespace(event=SimpleNamespace(user_id=None, user=None))
    assert get_provider_for_prospect(p, fallback) is fallback


def test_routing_falls_back_when_user_has_no_account():
    fallback = object()
    user = SimpleNamespace(id=1, unipile_account_id=None)
    p = SimpleNamespace(event=SimpleNamespace(user_id=1, user=user))
    assert get_provider_for_prospect(p, fallback) is fallback


def test_routing_returns_per_user_provider_when_user_has_account(monkeypatch):
    """Happy path: a prospect whose event-owner has a live Unipile account
    routes the send through THAT user's provider, not the env-var fallback."""
    monkeypatch.setenv("UNIPILE_DSN", "https://api.test.unipile")
    monkeypatch.setenv("UNIPILE_API_KEY", "test-key")
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    fallback = object()
    user = SimpleNamespace(id=1, unipile_account_id="user_account_42")
    p = SimpleNamespace(event=SimpleNamespace(user_id=1, user=user))
    routed = get_provider_for_prospect(p, fallback)
    assert routed is not fallback
    assert isinstance(routed, UnipileProvider)
    assert routed.account_id == "user_account_42"


def test_routing_falls_back_when_per_user_resolution_raises(monkeypatch):
    """If get_provider_for_user raises (e.g. stale connection, missing env),
    we must NOT crash the webhook : fall back to the operator account."""
    fallback = object()
    user = SimpleNamespace(id=1, unipile_account_id="user_account")

    def _boom(*a, **k):
        raise RuntimeError("stale")

    monkeypatch.setattr("backend.providers.get_provider_for_user", _boom)
    p = SimpleNamespace(event=SimpleNamespace(user_id=1, user=user))
    assert get_provider_for_prospect(p, fallback) is fallback


# ── UnipileProvider.fetch_thread dry-run fixture ───────────────────────

def test_fetch_thread_dry_run_returns_fixture():
    """The dry-run fixture lets the reply-agent harness be exercised
    end-to-end without Unipile. Pin its shape so a refactor breaks the
    test instead of the demo."""
    provider = UnipileProvider(dry_run=True)
    thread = provider.fetch_thread("any_chat_id")
    assert len(thread) == 2
    assert {m["direction"] for m in thread} == {"outbound", "inbound"}
    for m in thread:
        assert "text" in m
        assert "ts" in m


def test_fetch_thread_empty_chat_id_returns_empty_in_live_mode():
    """Live mode with no chat_id should return [] without hitting the
    network. The agent harness handles empty threads gracefully."""
    provider = UnipileProvider(dry_run=False)
    assert provider.fetch_thread("") == []
    assert provider.fetch_thread(None) == []
