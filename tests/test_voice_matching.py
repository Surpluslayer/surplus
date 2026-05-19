"""
Tests for voice-matching : per-operator style examples that get injected
into compose()'s prompt as <style_examples> so Claude mirrors the
operator's voice.

Resolution order in compose():
  1. event.user.voice_examples (JSON list on User row)
  2. OPERATOR_VOICE_EXAMPLES env var (JSON list)
  3. [] : compose falls back to generic personalization
"""
from __future__ import annotations
import json
from types import SimpleNamespace

import pytest

from backend.agents import outreach


@pytest.fixture
def event_with_user():
    user = SimpleNamespace(
        id=1, unipile_account_id="op", name="Daniel",
        voice_examples="",
    )
    return SimpleNamespace(
        id=1, user=user,
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )


# ── _get_voice_examples resolution ──────────────────────────────────────

def test_no_user_examples_no_env_returns_empty(event_with_user, monkeypatch):
    monkeypatch.delenv("OPERATOR_VOICE_EXAMPLES", raising=False)
    assert outreach._get_voice_examples(event_with_user) == []


def test_env_var_used_when_user_row_empty(event_with_user, monkeypatch):
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES",
                       json.dumps(["env example 1", "env example 2"]))
    examples = outreach._get_voice_examples(event_with_user)
    assert examples == ["env example 1", "env example 2"]


def test_user_row_takes_precedence_over_env(event_with_user, monkeypatch):
    """Per-operator examples win over the global env-var fallback."""
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", json.dumps(["env"]))
    event_with_user.user.voice_examples = json.dumps(["user_row"])
    examples = outreach._get_voice_examples(event_with_user)
    assert examples == ["user_row"]


def test_malformed_json_treated_as_empty(event_with_user, monkeypatch):
    """A typo in the env var shouldn't crash compose. Silently treat as
    'no examples' so outreach keeps working."""
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", "{not json")
    assert outreach._get_voice_examples(event_with_user) == []


def test_examples_capped_at_eight(event_with_user, monkeypatch):
    """Input tokens are bounded : we trim to 8 examples max regardless
    of how many the operator pasted."""
    monkeypatch.setenv(
        "OPERATOR_VOICE_EXAMPLES",
        json.dumps([f"example {i}" for i in range(20)]),
    )
    examples = outreach._get_voice_examples(event_with_user)
    assert len(examples) == 8


def test_empty_strings_stripped_from_list(event_with_user, monkeypatch):
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES",
                       json.dumps(["real one", "", "  ", "another"]))
    examples = outreach._get_voice_examples(event_with_user)
    assert examples == ["real one", "another"]


def test_non_list_json_treated_as_empty(event_with_user, monkeypatch):
    """A JSON object or string at the top level isn't valid voice examples."""
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", json.dumps({"key": "value"}))
    assert outreach._get_voice_examples(event_with_user) == []


# ── compose user-message injection ──────────────────────────────────────

def test_user_message_includes_style_block_when_examples_present(event_with_user):
    prospect = SimpleNamespace(name="Maya", role="Eng", company="Lo91r",
                               works_on="observability", offers="depth",
                               headline="")
    examples = ["Hey Maya, your work caught my eye...",
                "Hi Theo, the distributed systems space..."]
    msg = outreach._compose_user_message(
        prospect, event_with_user, host_bio=None,
        framing="a dinner in SF", voice_examples=examples,
    )
    assert "<style_examples>" in msg
    assert "</style_examples>" in msg
    assert examples[0] in msg
    assert examples[1] in msg


def test_user_message_omits_style_block_when_no_examples(event_with_user):
    """If we have no examples, don't render an empty style block : it's
    noise that the model has to parse."""
    prospect = SimpleNamespace(name="Maya", role="Eng", company="Lo91r",
                               works_on="observability", offers="depth",
                               headline="")
    msg = outreach._compose_user_message(
        prospect, event_with_user, host_bio=None,
        framing="a dinner in SF", voice_examples=[],
    )
    assert "<style_examples>" not in msg
