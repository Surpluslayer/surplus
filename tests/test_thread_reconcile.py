"""Tests for spine-vs-thread reconciliation (stale next_step filtering)."""
from __future__ import annotations

from backend.agents.relationship.pipeline.context.reconcile import (
    apply_to_facts,
    clear_prospect_next_step_if_fulfilled,
    message_addresses_obligation,
    obligation_still_open,
    reconcile_next_step,
    window_thread,
)


def test_obligation_open_when_no_outbound():
    assert obligation_still_open("send the pricing deck", []) is True
    assert reconcile_next_step("send the pricing deck", []) == "send the pricing deck"


def test_obligation_closed_on_delivery_message():
    prior = [{"who": "host", "text": "Here's the pricing deck you asked for."}]
    assert obligation_still_open("send the pricing deck", prior) is False
    assert reconcile_next_step("send the pricing deck", prior) is None


def test_obligation_closed_when_woven_into_first_message():
    step = "grab a coffee — book a time: calendly.com/me/15min"
    prior = [{"who": "host", "text": f"Great meeting you! {step}"}]
    assert obligation_still_open(step, prior) is False


def test_promise_only_does_not_close_capture_next_step():
    prior = [{"who": "host", "text": "I'll intro you to Mara this week."}]
    assert obligation_still_open("send intro to Mara", prior) is True
    assert message_addresses_obligation(prior[0]["text"], "send intro to Mara") is False


def test_delivery_closes_matching_obligation():
    prior = [{"who": "host", "text": "As promised, here's the deck."}]
    assert obligation_still_open("send the deck", prior) is False


def test_apply_to_facts_strips_stale_next_step():
    facts = {"next_step": "send demo video", "met_at": "Milken"}
    prior = [{"who": "host", "text": "Sending you the demo video now."}]
    out = apply_to_facts(facts, prior)
    assert out["next_step"] is None
    assert out["met_at"] == "Milken"


def test_window_thread_keeps_recent_and_summarizes_older():
    prior = [{"who": "host", "text": f"msg {i}"} for i in range(8)]
    recent, summary = window_thread(prior, recent=3)
    assert len(recent) == 3
    assert recent[0]["text"] == "msg 5"
    assert summary and "Earlier conversation" in summary
    assert "msg 0" in summary


def test_clear_prospect_next_step_if_fulfilled():
    class P:
        next_step = "send the deck"

    p = P()
    assert clear_prospect_next_step_if_fulfilled(p, "Here's the deck.") is True
    assert p.next_step is None
