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

def test_voice_context_shape():
    user = SimpleNamespace(voice_examples=json.dumps(["Hey Mia! great to meet you"]),
                           voice_profile="")
    ctx = voice.build_voice_context(user, channel="linkedin",
                                    message_type="warm_followup")
    assert ctx["examples"] == ["Hey Mia! great to meet you"]
    # profile is now populated (Step 2) and the block carries BOTH layers.
    assert isinstance(ctx["profile"], dict)
    assert ctx["profile"]["greeting"] == "hey"
    assert "<host_voice_profile>" in ctx["block"]
    assert "<style_examples>" in ctx["block"]
    # the profile block precedes the style-examples block
    assert ctx["block"].index("<host_voice_profile>") < ctx["block"].index("<style_examples>")


def test_voice_context_ignores_channel_and_type():
    """channel/message_type are accepted but must not change output yet."""
    user = SimpleNamespace(voice_examples=json.dumps(["hi"]), voice_profile="")
    a = voice.build_voice_context(user, channel="linkedin", message_type="x")
    b = voice.build_voice_context(user, channel="email", message_type="y")
    assert a == b


def test_voice_context_no_examples_is_empty_block():
    user = SimpleNamespace(voice_examples="", voice_profile="")
    ctx = voice.build_voice_context(user)
    assert ctx["examples"] == [] and ctx["profile"] is None and ctx["block"] == ""


# ── build_host_voice_profile / fingerprint / render ──────────────────────────

def test_profile_none_when_no_examples():
    assert voice.build_host_voice_profile([]) is None
    assert voice.build_host_voice_profile(["", "   "]) is None


def test_profile_captures_casual_voice():
    p = voice.build_host_voice_profile([
        "Hey Sarah! great meeting you, lets grab coffee soon 🙌",
        "hey, you free next week? would love to catch up!",
    ])
    assert p["greeting"] == "hey"
    assert p["uses_emoji"] is True and "🙌" in p["emoji_samples"]
    assert p["formality"] == "casual"
    assert p["length_band"] == "short"


def test_profile_captures_formal_voice():
    p = voice.build_host_voice_profile([
        "Hello Dr. Chen, thank you for the thoughtful conversation at the summit. "
        "I would welcome the opportunity to continue it. Best regards.",
        "Hello Mr. Patel, it was a pleasure connecting. Looking forward to staying "
        "in touch over the coming months. Kind regards.",
    ])
    assert p["greeting"] == "hello"
    assert p["uses_emoji"] is False
    assert p["formality"] in ("neutral", "formal")


def test_fingerprint_is_stable_and_order_sensitive():
    a = voice.fingerprint_examples(["one", "two"])
    assert a == voice.fingerprint_examples(["one", "two"])     # stable
    assert a != voice.fingerprint_examples(["two", "one"])     # order-sensitive
    assert a != voice.fingerprint_examples(["one"])            # content-sensitive


def test_render_profile_block_empty_when_none():
    assert voice.render_voice_profile_block(None) == ""


def test_render_profile_block_states_rules():
    p = voice.build_host_voice_profile(["Hey! thanks so much, talk soon 🙌"])
    block = voice.render_voice_profile_block(p)
    assert block.startswith("\n<host_voice_profile>")
    assert "</host_voice_profile>" in block
    assert "Typical length" in block
    assert "Hey" in block


# ── resolve_voice_profile_for_user (the cache seam) ──────────────────────────

def test_resolve_profile_builds_inline_when_no_cache():
    user = SimpleNamespace(voice_profile="")
    examples = ["Hey there! good to meet you"]
    prof = voice.resolve_voice_profile_for_user(user, examples)
    assert prof == voice.build_host_voice_profile(examples)


def test_resolve_profile_uses_cache_when_fingerprint_matches():
    examples = ["Hey! nice to meet you"]
    fp = voice.fingerprint_examples(examples)
    cached = {"fingerprint": fp, "profile": {"sentinel": "from-cache"}}
    user = SimpleNamespace(voice_profile=json.dumps(cached))
    assert voice.resolve_voice_profile_for_user(user, examples) == {"sentinel": "from-cache"}


def test_resolve_profile_ignores_stale_cache():
    """A cached profile whose fingerprint no longer matches the examples is
    discarded and rebuilt inline."""
    cached = {"fingerprint": "deadbeefdeadbeef", "profile": {"sentinel": "stale"}}
    user = SimpleNamespace(voice_profile=json.dumps(cached))
    examples = ["Hey! totally different examples now"]
    prof = voice.resolve_voice_profile_for_user(user, examples)
    assert prof == voice.build_host_voice_profile(examples)
    assert prof != {"sentinel": "stale"}


def test_resolve_profile_survives_bad_cache_json():
    user = SimpleNamespace(voice_profile="{not json")
    examples = ["Hey! hello"]
    assert voice.resolve_voice_profile_for_user(user, examples) == \
        voice.build_host_voice_profile(examples)
