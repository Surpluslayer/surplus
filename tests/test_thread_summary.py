"""Tests for LLM rolling thread summaries (cache + fallback)."""
from __future__ import annotations

from backend.agents.relationship.thread_reconcile import window_thread
from backend.agents.relationship.thread_summary import (
    _fingerprint,
    summarize_older_messages,
    window_and_summarize,
)


def test_fingerprint_changes_when_older_slice_changes():
    older_a = [{"who": "host", "when": "1", "text": "hello"}]
    older_b = [{"who": "host", "when": "1", "text": "hello"},
               {"who": "them", "when": "2", "text": "hi back"}]
    assert _fingerprint(older_a, 5) != _fingerprint(older_b, 5)


def test_summarize_falls_back_without_llm():
    older = [{"who": "host", "text": "Sent the pricing deck as promised."}]
    out = summarize_older_messages(older, recent_limit=5)
    assert out and "Earlier conversation" in out


def test_window_and_summarize_without_db():
    prior = [{"who": "host", "text": f"msg {i}"} for i in range(8)]
    recent, summary = window_and_summarize(prior, recent=3)
    assert len(recent) == 3
    assert summary and "Earlier conversation" in summary


def test_window_thread_still_splits_deterministically():
    """Legacy window_thread remains for lightweight callers/tests."""
    prior = [{"who": "host", "text": f"msg {i}"} for i in range(8)]
    recent, summary = window_thread(prior, recent=3)
    assert len(recent) == 3
    assert summary
