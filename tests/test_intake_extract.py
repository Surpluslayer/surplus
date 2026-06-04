"""tests/test_intake_extract.py : NL event description -> intake profile.

Covers the normalizer (snap-to-chip-vocab, drop-unmapped, clamps, co_stage/
event_name passthrough), the one-shot extractor's fail-soft contract, and the
mode-less /events/intake/from-text route. The Anthropic call is replaced by a
fake client so the suite runs offline.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.triage import intake_extract as ie


# ── fake Anthropic client ────────────────────────────────────────────────────
class _FakeMsg:
    def __init__(self, text):
        self.content = [SimpleNamespace(text=text)]


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **_kw):
        return _FakeMsg(json.dumps(self._payload))


# ── normalizer ───────────────────────────────────────────────────────────────
def test_normalize_snaps_enums_and_drops_unmapped():
    raw = {
        "role": "  ML infra founders  ",
        "seniority": ["Leadership", "VP-level"],   # VP-level not in vocab → dropped
        "co_stage": ["seed"],                       # case-insensitive match
        "yoe": ["6-10"],
        "format": "fireside",                       # not an exact option → dropped
        "city": "San Francisco",
        "goal": ["Fundraising", "world domination"],
        "sources": ["github", "twitter"],
    }
    out = ie._normalize_profile(raw)
    assert out["role"] == "ML infra founders"
    assert out["seniority"] == ["Leadership"]
    assert out["co_stage"] == ["Seed"]
    assert out["yoe"] == ["6-10"]
    assert "format" not in out                       # 'fireside' isn't a chip value
    assert out["city"] == "San Francisco"
    assert out["goal"] == ["Fundraising"]
    assert out["sources"] == ["github"]


def test_normalize_clamps_numbers_and_keeps_order():
    out = ie._normalize_profile({
        "headcount": 9999, "budget": -50,
        "seniority": ["Staff+", "Student"],          # returned in vocab order
    })
    assert out["headcount"] == 160                    # clamped to max
    assert out["budget"] == 0                         # clamped to min
    assert out["seniority"] == ["Student", "Staff+"]  # SENIORITY order, not input order


def test_normalize_omits_absent_fields():
    out = ie._normalize_profile({"city": "NYC"})
    assert out == {"city": "NYC"}                     # nothing invented


def test_normalize_format_single_value():
    assert ie._normalize_profile({"format": "Mixer"})["format"] == "Mixer"
    assert "format" not in ie._normalize_profile({"format": ["Mixer"]})  # list → not a single str


# ── extractor (fail-soft) ────────────────────────────────────────────────────
def test_extract_empty_description_is_error():
    res = ie.extract_intake_profile("   ")
    assert res.profile == {} and res.error == "empty description"


def test_extract_without_api_key_is_soft_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = ie.extract_intake_profile("a dinner for founders")
    assert res.profile == {} and "ANTHROPIC_API_KEY" in res.error


def test_extract_roundtrip_with_fake_client():
    fake = _FakeClient({
        "role": "ML infrastructure founders",
        "seniority": ["Leadership"],
        "co_stage": ["Seed"],
        "format": "Sit-down dinner",
        "city": "San Francisco",
        "headcount": 40,
        "goal": ["Hiring pipeline"],
        "summary": "An intimate seed-stage ML-infra founder dinner in SF.",
    })
    res = ie.extract_intake_profile("...", client=fake)
    assert res.error == ""
    assert res.profile["role"] == "ML infrastructure founders"
    assert res.profile["format"] == "Sit-down dinner"
    assert res.profile["headcount"] == 40
    assert "SF" in res.summary


def test_extract_non_json_output_is_soft_error():
    fake = _FakeClient(None)
    # payload None → json.dumps("null") → not a dict → soft error
    fake._payload = "not json at all"

    class _Raw:
        def __init__(self, c):
            self.content = [SimpleNamespace(text=c)]

    fake.messages.create = lambda **_k: _Raw("totally not json {")
    res = ie.extract_intake_profile("x", client=fake)
    assert res.profile == {} and res.error


# ── route ────────────────────────────────────────────────────────────────────
def test_intake_from_text_route(monkeypatch):
    from backend.routes import triage as routes
    fake = _FakeClient({
        "role": "robotics founders",
        "co_stage": ["Pre-seed"],
        "format": "Hackathon",
        "summary": "A pre-seed robotics hackathon.",
    })
    monkeypatch.setattr(ie, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    user = SimpleNamespace(id=1)
    resp = routes.intake_from_text(routes.IntakeFromTextBody(description="..."), user)
    assert resp.profile.role == "robotics founders"
    assert resp.profile.co_stage == ["Pre-seed"]
    assert resp.profile.format == "Hackathon"
    assert resp.profile.city is None              # absent → null, frontend keeps default
    assert "robotics" in resp.summary


# ── multi-turn interview (run_intake_turn) ────────────────────────────────────
def test_turn_empty_conversation_is_error():
    res = ie.run_intake_turn([])
    assert res.complete is False and res.error == "empty conversation"


def test_turn_without_api_key_is_soft_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = ie.run_intake_turn([{"role": "user", "content": "a dinner"}])
    assert res.complete is False and "ANTHROPIC_API_KEY" in res.error


def test_turn_ask_returns_question_not_complete():
    fake = _FakeClient({"action": "ask",
                        "question": "Who is the room for — founders, engineers?"})
    res = ie.run_intake_turn([{"role": "user", "content": "an event"}], client=fake)
    assert res.complete is False
    assert "room" in res.question
    assert res.profile == {} and res.triage_config == {}
    assert res.assistant_json  # raw reply preserved for transcript replay


def test_turn_ask_without_question_falls_back():
    fake = _FakeClient({"action": "ask"})  # malformed: no question text
    res = ie.run_intake_turn([{"role": "user", "content": "hi"}], client=fake)
    assert res.complete is False and res.question  # never an empty bubble


def test_turn_finalize_fills_profile_and_config():
    fake = _FakeClient({
        "action": "finalize",
        "role": "ML infrastructure founders",
        "seniority": ["Leadership"],
        "co_stage": ["Seed"],
        "format": "Sit-down dinner",
        "city": "San Francisco",
        "headcount": 40,
        "goal": ["Hiring pipeline"],
        "anti_fit": ["recruiters prospecting for hires"],
        "priority_archetypes": ["founder"],
        "summary": "An intimate seed-stage ML-infra founder dinner in SF.",
    })
    res = ie.run_intake_turn(
        [{"role": "user", "content": "dinner for ML infra founders"}], client=fake)
    assert res.complete is True and res.error == ""
    assert res.profile["role"] == "ML infrastructure founders"
    assert res.profile["format"] == "Sit-down dinner"
    assert res.triage_config  # compiled rich ICP rode along
    assert any("anti-fit" in c for c in res.captured)


def test_turn_missing_action_is_treated_as_finalize():
    # The one-shot extractor commits when no action is given; the turn path
    # keeps that bias so a rich first message finalizes immediately.
    fake = _FakeClient({"role": "robotics founders", "format": "Hackathon"})
    res = ie.run_intake_turn([{"role": "user", "content": "x"}], client=fake)
    assert res.complete is True
    assert res.profile["role"] == "robotics founders"


def test_turn_drops_malformed_messages():
    convo = [
        {"role": "system", "content": "ignore me"},   # bad role → dropped
        {"role": "user", "content": ""},               # empty → dropped
        {"role": "user"},                              # no content → dropped
        {"role": "user", "content": "a real message"},
    ]
    assert ie._sanitize_messages(convo) == [{"role": "user", "content": "a real message"}]


def test_intake_turn_route(monkeypatch):
    from backend.routes import triage as routes
    fake = _FakeClient({"action": "ask", "question": "What's the goal of the event?"})
    monkeypatch.setattr(ie, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    user = SimpleNamespace(id=1)
    body = routes.IntakeTurnBody(messages=[
        routes.IntakeTurnMessage(role="user", content="I'm hosting something"),
    ])
    resp = routes.intake_turn(body, user)
    assert resp.complete is False
    assert "goal" in resp.question
    assert resp.profile.role is None  # nothing filled on a question turn
