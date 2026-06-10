"""
Tests for the advisor "Your book today" engine (agents/book.py) and its demo
book. Runs entirely on the deterministic (no-ANTHROPIC_API_KEY) path so the
surface is verified end-to-end without a live key.
"""
from __future__ import annotations

import os

import pytest

from backend.agents import book as b
from backend.routes.book import _demo_book


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Force the deterministic heuristic path (what the demo runs).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


def test_demo_book_today_feed_shape():
    feed = b.build_today(_demo_book())
    assert set(feed) >= {"date", "updates", "needs_outreach"}
    # The three mockup updates, newest-first.
    names = [u["name"] for u in feed["updates"]]
    assert names[:3] == ["James Holloway", "Priya Nadel", "David Osei"]
    for u in feed["updates"]:
        assert u["headline"] and u["can_draft"] is True
    # VIP stars carried through.
    assert {u["name"]: u["vip"] for u in feed["updates"]}["James Holloway"] is True


def test_needs_outreach_excludes_recently_active():
    feed = b.build_today(_demo_book())
    needs_names = {n["name"] for n in feed["needs_outreach"]}
    # People who just had an update are NOT also flagged as overdue.
    assert "James Holloway" not in needs_names
    assert "David Osei" not in needs_names
    # The mockup's overdue names are present.
    assert {"Thomas Reyes", "Margaret Chen", "Sofia Klein"} <= needs_names
    assert len(feed["needs_outreach"]) == 9


def test_review_due_always_needs_outreach_with_reason():
    margaret = next(c for c in _demo_book() if c["name"] == "Margaret Chen")
    h = b.score_health(margaret)
    assert h["needs_outreach"] is True
    assert "review due" in h["reason"].lower()


def test_quiet_reason_reflects_days():
    thomas = next(c for c in _demo_book() if c["name"] == "Thomas Reyes")
    h = b.score_health(thomas)
    assert h["reason"] == "Quiet 64 days"
    assert h["status"] in ("cooling", "dormant")


def test_active_contact_does_not_need_outreach():
    james = next(c for c in _demo_book() if c["name"] == "James Holloway")
    h = b.score_health(james)
    assert h["needs_outreach"] is False


def test_detect_update_passthrough_and_none():
    david = next(c for c in _demo_book() if c["name"] == "David Osei")
    u = b.detect_update(david)
    assert u and u["type"] == "fundraise" and u["headline"] == "Raised a new fund"
    # A contact with no signals yields no update.
    assert b.detect_update({"name": "Nobody"}) is None


def test_draft_congratulation_vs_reengage():
    c = {"name": "Priya Nadel", "title": "Principal", "firm": "Lumen Growth",
         "interaction_history": ""}
    warm = b.draft_message(c, "Promoted to MD, Lumen Growth", channel="email",
                           user_name="Jordan")
    assert warm["body"] and warm["subject"]  # email returns a subject
    cold = b.draft_message(c, "Quiet 64 days", channel="sms", user_name="Jordan")
    assert cold["subject"] is None           # non-email: no subject
    assert "Priya" in cold["body"]


def test_ask_agent_routes_queries():
    book = _demo_book()
    cooling = b.ask_agent(book, "who's cooling?")
    assert cooling["people"] and "cooling" in cooling["answer"].lower() \
        or "overdue" in cooling["answer"].lower()
    reviews = b.ask_agent(book, "reviews due")
    names = {p["name"] for p in reviews["people"]}
    assert "Margaret Chen" in names  # she has review_due=True
