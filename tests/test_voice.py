"""Unit tests for the shared voice layer (backend/agents/voice.py).

Step 0 is behavior-preserving: these pin the parse/cap/fallback semantics and
the <style_examples> block format that the cold-DM composer and the follow-up
agent now share, so a future change to one surface can't silently change voice
resolution for the other.
"""
from __future__ import annotations

import json

from types import SimpleNamespace

from backend.agents import voice


# ── parse_voice_examples ─────────────────────────────────────────────────────

def test_parse_valid_list():
    raw = json.dumps(["hey there", "thanks so much"])
    assert voice.parse_voice_examples(raw, env_fallback=False) == [
        "hey there", "thanks so much"]


def test_parse_empty_no_env_is_empty():
    assert voice.parse_voice_examples("", env_fallback=False) == []
    assert voice.parse_voice_examples(None, env_fallback=False) == []


def test_parse_bad_json_is_empty():
    assert voice.parse_voice_examples("{not json", env_fallback=False) == []


def test_parse_non_list_json_is_empty():
    assert voice.parse_voice_examples('{"a": 1}', env_fallback=False) == []


def test_parse_strips_and_drops_blanks():
    raw = json.dumps(["  spaced  ", "", "   ", "real"])
    assert voice.parse_voice_examples(raw, env_fallback=False) == ["spaced", "real"]


def test_parse_caps_at_limit():
    raw = json.dumps([f"m{i}" for i in range(20)])
    out = voice.parse_voice_examples(raw, env_fallback=False)
    assert len(out) == 8
    assert out[0] == "m0" and out[-1] == "m7"


def test_parse_env_fallback_used_when_raw_empty(monkeypatch):
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", json.dumps(["from env"]))
    assert voice.parse_voice_examples("", env_fallback=True) == ["from env"]


def test_parse_env_fallback_disabled(monkeypatch):
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", json.dumps(["from env"]))
    assert voice.parse_voice_examples("", env_fallback=False) == []


# ── resolve_voice_examples_for_user ──────────────────────────────────────────

def test_resolve_from_user_row():
    user = SimpleNamespace(voice_examples=json.dumps(["row msg"]))
    assert voice.resolve_voice_examples_for_user(user) == ["row msg"]


def test_resolve_none_user_no_env_is_empty(monkeypatch):
    monkeypatch.delenv("OPERATOR_VOICE_EXAMPLES", raising=False)
    assert voice.resolve_voice_examples_for_user(None) == []


def test_resolve_detached_instance_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OPERATOR_VOICE_EXAMPLES", json.dumps(["env msg"]))

    class Detached:
        @property
        def voice_examples(self):  # noqa: D401 - simulates DetachedInstanceError
            raise RuntimeError("DetachedInstanceError")

    assert voice.resolve_voice_examples_for_user(Detached()) == ["env msg"]


# ── build_style_examples_block ───────────────────────────────────────────────

def test_block_empty_when_no_examples():
    assert voice.build_style_examples_block([]) == ""


def test_block_matches_prior_voice_block_format():
    """Byte-for-byte the follow-up agent's previous _voice_block output, so the
    shared formatter is a drop-in (note the intentional leading newline)."""
    examples = ["hey!", "thanks"]
    expected = "\n".join([
        "", "<style_examples>",
        "Past messages this host actually sent. Match their VOICE — greeting, "
        "sign-off, sentence length, formality, punctuation and emoji habits — "
        "not the content:",
        "---\nExample 1:\nhey!",
        "---\nExample 2:\nthanks",
        "---", "</style_examples>",
    ])
    assert voice.build_style_examples_block(examples) == expected


# ── build_voice_context (the Step-2/4 seam) ──────────────────────────────────

def test_voice_context_shape_v0():
    user = SimpleNamespace(voice_examples=json.dumps(["hi"]))
    ctx = voice.build_voice_context(user, channel="linkedin",
                                    message_type="warm_followup")
    assert ctx["profile"] is None                 # filled in Step 2
    assert ctx["examples"] == ["hi"]
    assert ctx["block"] == voice.build_style_examples_block(["hi"])


def test_voice_context_ignores_channel_and_type_in_v0():
    """channel/message_type are accepted but must not change output yet."""
    user = SimpleNamespace(voice_examples=json.dumps(["hi"]))
    a = voice.build_voice_context(user, channel="linkedin", message_type="x")
    b = voice.build_voice_context(user, channel="email", message_type="y")
    assert a == b
