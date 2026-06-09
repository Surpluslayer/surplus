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


# ── Step 4: provenance records + scoped retrieval ────────────────────────────

def test_records_accept_both_legacy_and_tagged_shapes():
    """A plain string is a channel-agnostic record; a dict carries provenance."""
    raw = json.dumps([
        "legacy plain string",
        {"text": "tagged linkedin", "channel": "LinkedIn", "message_type": "Cold_Intro"},
        {"message": "alt body key", "type": "warm_followup"},  # message/type aliases
    ])
    recs = voice.parse_voice_records(raw, env_fallback=False)
    assert recs[0] == {"text": "legacy plain string", "channel": None,
                       "message_type": None}
    # tags are lowercased
    assert recs[1] == {"text": "tagged linkedin", "channel": "linkedin",
                       "message_type": "cold_intro"}
    assert recs[2] == {"text": "alt body key", "channel": None,
                       "message_type": "warm_followup"}


def test_dict_record_without_text_is_dropped():
    raw = json.dumps([{"channel": "linkedin"}, {"text": "  ", "channel": "x"},
                      {"text": "kept"}])
    assert voice.parse_voice_examples(raw, env_fallback=False) == ["kept"]


def test_legacy_plain_strings_are_unaffected_by_scoping():
    """Untagged examples are channel-agnostic: scoping to any channel returns
    them unchanged, so existing plain-string data behaves exactly as before."""
    raw = json.dumps(["one", "two", "three"])
    unscoped = voice.parse_voice_examples(raw, env_fallback=False)
    scoped = voice.parse_voice_examples(raw, env_fallback=False,
                                        channel="linkedin", message_type="cold_intro")
    assert unscoped == scoped == ["one", "two", "three"]


def test_scoping_filters_to_matching_channel():
    raw = json.dumps([
        {"text": "li one", "channel": "linkedin"},
        {"text": "email one", "channel": "email"},
        {"text": "li two", "channel": "linkedin"},
    ])
    assert voice.parse_voice_examples(raw, env_fallback=False, channel="linkedin") \
        == ["li one", "li two"]
    assert voice.parse_voice_examples(raw, env_fallback=False, channel="email") \
        == ["email one"]


def test_untagged_records_remain_eligible_under_scope():
    """An untagged example applies to every channel, alongside the matching ones."""
    raw = json.dumps([
        {"text": "agnostic"},                              # no channel
        {"text": "li only", "channel": "linkedin"},
        {"text": "email only", "channel": "email"},
    ])
    assert voice.parse_voice_examples(raw, env_fallback=False, channel="linkedin") \
        == ["agnostic", "li only"]


def test_scope_falls_back_to_all_when_channel_has_no_examples():
    """Scoping to a channel the host has zero examples for must NOT yield an empty
    voice block — fall back to the full set rather than dropping voice entirely."""
    raw = json.dumps([
        {"text": "li one", "channel": "linkedin"},
        {"text": "li two", "channel": "linkedin"},
    ])
    assert voice.parse_voice_examples(raw, env_fallback=False, channel="email") \
        == ["li one", "li two"]


def test_message_type_narrows_within_channel():
    raw = json.dumps([
        {"text": "cold li", "channel": "linkedin", "message_type": "cold_intro"},
        {"text": "warm li", "channel": "linkedin", "message_type": "warm_followup"},
        {"text": "email warm", "channel": "email", "message_type": "warm_followup"},
    ])
    assert voice.parse_voice_examples(
        raw, env_fallback=False, channel="linkedin", message_type="warm_followup") \
        == ["warm li"]


def test_scope_applied_before_cap():
    """The cap bounds the SELECTED set, not the raw list, so a channel still gets
    up to `limit` of its own examples even when other-channel rows come first."""
    rows = [{"text": f"email{i}", "channel": "email"} for i in range(8)]
    rows += [{"text": f"li{i}", "channel": "linkedin"} for i in range(3)]
    raw = json.dumps(rows)
    assert voice.parse_voice_examples(raw, env_fallback=False, limit=8,
                                      channel="linkedin") == ["li0", "li1", "li2"]


def test_build_voice_context_scopes_examples_and_profile():
    user = SimpleNamespace(
        voice_examples=json.dumps([
            {"text": "Hey! quick one, you around? 🙌", "channel": "linkedin"},
            {"text": "Dear Sir, I write to formally request. Best regards.",
             "channel": "email"},
        ]),
        voice_profile="")
    ctx = voice.build_voice_context(user, channel="linkedin")
    assert ctx["examples"] == ["Hey! quick one, you around? 🙌"]
    # profile is distilled from the SCOPED example, so it reads casual not formal
    assert ctx["profile"]["greeting"] == "hey"
    assert ctx["profile"]["uses_emoji"] is True


# ── contact register detection (orthogonal to host identity) ─────────────────

def test_detect_register_none_when_empty():
    assert voice.detect_register([]) is None
    assert voice.detect_register(["   ", ""]) is None


def test_detect_register_formal_on_letter_style():
    msg = ("Dear Alex, thank you for the kind note. I would welcome the "
           "opportunity to discuss your work further. Might you have "
           "availability next week to speak? Kind regards, Dr. Patel")
    assert voice.detect_register([msg]) == "formal"


def test_detect_register_casual_on_emoji_and_slang():
    assert voice.detect_register(["omg yesss so good meeting you!! lmk 🙌"]) == "casual"
    assert voice.detect_register(["hey! lets grab coffee soon, gonna be in town"]) == "casual"


def test_detect_register_neutral_when_no_strong_signal():
    msg = "Thanks for reaching out. Happy to chat next week, what works for you?"
    assert voice.detect_register([msg]) == "neutral"


def test_register_guidance_maps_each_label_and_none():
    assert voice.register_guidance(None) is None
    assert "no emoji" in voice.register_guidance("formal")
    assert "casual voice fits" in voice.register_guidance("casual")
    assert "neutral register" in voice.register_guidance("neutral")


def test_register_is_the_contacts_voice_not_the_hosts():
    """Register is judged from the CONTACT's messages only; a casual host
    profile must not flip a formal contact's detected register."""
    formal_contact = ["Dear Sir, I would be grateful for your guidance. Kind regards."]
    assert voice.detect_register(formal_contact) == "formal"
