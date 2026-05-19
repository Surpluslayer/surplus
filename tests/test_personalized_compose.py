"""
Tests for the LLM-personalized compose() in agents/outreach.py.

compose() now calls Claude (Haiku) by default and falls back to the
deterministic template on any failure. These tests pin both paths so
a model outage or env hiccup can't break outreach.
"""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.agents import outreach


@pytest.fixture
def fake_event():
    return SimpleNamespace(
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )


@pytest.fixture
def fake_prospect():
    return SimpleNamespace(
        name="Maya Rodriguez", role="Staff Infra Engineer", company="Lo91r",
        works_on="observability", offers="Observability depth",
        headline="Distributed systems @ Lo91r", seeks="Staff-scope role",
    )


# ── Fallback path : Claude unavailable / fails ────────────────────────

def test_falls_back_to_template_when_no_api_key(fake_event, fake_prospect, monkeypatch):
    """No ANTHROPIC_API_KEY in env : compose should produce template output
    without ever instantiating an Anthropic client."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = outreach.compose(fake_prospect, fake_event)
    # Template output contains a specific hard-coded phrase
    assert "Worth your time?" in msg.note
    assert "Thanks for connecting, Maya." in msg.message


def test_falls_back_to_template_when_disable_flag_set(fake_event, fake_prospect, monkeypatch):
    """OUTREACH_COMPOSE_DISABLE=1 forces template even with API key present.
    Escape hatch for cost spikes / model issues."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("OUTREACH_COMPOSE_DISABLE", "1")
    with patch("backend.agents.outreach._compose_via_claude") as via_claude:
        msg = outreach.compose(fake_prospect, fake_event)
    via_claude.assert_not_called()
    assert "Worth your time?" in msg.note


def test_falls_back_to_template_on_claude_exception(fake_event, fake_prospect, monkeypatch):
    """Network error / timeout / API outage : template fills in transparently."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    with patch("backend.agents.outreach._compose_via_claude", return_value=None):
        msg = outreach.compose(fake_prospect, fake_event)
    assert "Worth your time?" in msg.note


# ── LLM path : Claude returns a valid decision ────────────────────────

def test_uses_llm_output_when_claude_succeeds(fake_event, fake_prospect, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake_note = "Hi Maya, your observability work at Lo91r is exactly the audience we're pulling in."
    fake_msg = "Thanks for connecting. The dinner is 12 staff+ infra folks in SF; given your work on observability, you'd be a great fit. Worth a closer look?"
    with patch("backend.agents.outreach._compose_via_claude",
               return_value=(fake_note, fake_msg)):
        msg = outreach.compose(fake_prospect, fake_event)
    assert msg.note == fake_note
    assert msg.message == fake_msg


def test_truncates_note_when_llm_exceeds_limit(fake_event, fake_prospect, monkeypatch):
    """LLM occasionally ignores the 280-char limit. We hard-cap so LinkedIn
    won't reject the invite."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    too_long = "Hi Maya, " + "x" * 400 + "."
    with patch("backend.agents.outreach._compose_via_claude",
               return_value=(too_long, "short message")):
        msg = outreach.compose(fake_prospect, fake_event)
    assert len(msg.note) <= outreach.NOTE_CHAR_LIMIT


# ── User-message builder shape ────────────────────────────────────────

def test_compose_user_message_includes_recipient_facts(fake_event, fake_prospect):
    """Pin that the model sees the recipient's grounding facts."""
    msg = outreach._compose_user_message(
        fake_prospect, fake_event, host_bio=None,
        framing="a 40-person sit-down dinner in San Francisco",
    )
    assert "Maya Rodriguez" in msg
    assert "Staff Infra Engineer" in msg
    assert "Lo91r" in msg
    assert "observability" in msg


def test_compose_user_message_does_not_mention_peers(fake_event, fake_prospect):
    """Peer names are deliberately NOT in the prompt : the system prompt
    tells the model not to drop names, and removing them from context
    eliminates the temptation entirely."""
    msg = outreach._compose_user_message(
        fake_prospect, fake_event, host_bio=None,
        framing="a 40-person sit-down dinner in San Francisco",
    )
    assert "Already confirmed" not in msg
    assert "Peer reveal" not in msg


def test_compose_user_message_omits_empty_optional_fields(fake_event):
    """Empty optional fields shouldn't surface as 'unknown' strings :
    they're simply absent from the prompt."""
    bare = SimpleNamespace(name="Sam", role="Engineer", company="Acme",
                           works_on=None, offers=None, headline=None)
    msg = outreach._compose_user_message(
        bare, fake_event, host_bio=None, framing="a dinner",
    )
    assert "What they work on" not in msg
    assert "Offers / strengths" not in msg
    assert "Headline" not in msg


def test_template_fallback_does_not_name_peers(fake_event, fake_prospect, monkeypatch):
    """Template path also has peer-reveal removed for consistency."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = outreach.compose(fake_prospect, fake_event, peers=["Theo Lindqvist", "Alex Chen"])
    assert "Theo" not in msg.note
    assert "Alex" not in msg.note
    assert "already in" not in msg.note.lower()
