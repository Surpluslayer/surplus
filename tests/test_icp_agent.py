"""Tests for the conversational ICP builder (icp_agent).

Contract:
  - extract_icp / run_icp_turn return an ICPAgentResult.
  - On a "finalize" model reply -> complete=True, .icp normalized to compile_icp's
    recognized keys (junk keys dropped, types coerced), and .to_triage_config()
    yields a valid triage_config (round-trips through the deterministic compiler).
  - On an "ask" reply -> complete=False with .question set.
  - Fail-safe: no API key, a thrown client, or non-JSON output -> a result with
    .error and no exception; .to_triage_config() still works (empty -> minimal).
"""
from __future__ import annotations
from types import SimpleNamespace

import pytest

from backend.triage import icp_agent
from backend.triage.icp_agent import (
    ICPAgentResult, extract_icp, run_icp_turn, _normalize_icp)


class _FakeClient:
    """Stub Anthropic client returning a canned text block."""
    def __init__(self, payload: str):
        self.messages = SimpleNamespace(
            create=lambda **_: SimpleNamespace(
                content=[SimpleNamespace(text=payload)]))


class _BoomClient:
    def __init__(self):
        def _raise(**_):
            raise RuntimeError("network down")
        self.messages = SimpleNamespace(create=_raise)


# ── finalize path ─────────────────────────────────────────────────────────────

def test_finalize_returns_normalized_icp(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = (
        '{"action": "finalize", "summary": "Technical AI founders in NYC.", '
        '"icp": {"role": "technical AI founder", "format": "intimate fireside", '
        '"city": "NYC", "capacity": 50, "priority_archetypes": ["founder"], '
        '"deprioritize_archetypes": ["investor"], '
        '"anti_fit": ["recruiters prospecting for hires"], '
        '"require_corroboration": true, "garbage_key": "should be dropped"}}')
    res = extract_icp("intimate a16z fireside for AI founders in NYC, 50 seats",
                      client=_FakeClient(payload))
    assert res.complete is True
    assert res.icp["role"] == "technical AI founder"
    assert res.icp["capacity"] == 50
    assert res.icp["priority_archetypes"] == ["founder"]
    assert "garbage_key" not in res.icp        # hallucinated key dropped
    assert res.summary.startswith("Technical AI founders")


def test_finalized_icp_round_trips_through_compiler(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = (
        '{"action": "finalize", "icp": {"format": "intimate dinner", '
        '"city": "NYC", "priority_archetypes": ["founder"], '
        '"deprioritize_archetypes": ["investor"], "capacity": 30}}')
    res = extract_icp("dinner", client=_FakeClient(payload))
    cfg = res.to_triage_config()
    # Must be a valid triage_config the engine consumes.
    assert cfg["capacity"] == 30
    assert cfg["archetype_priority"]["boost"] == {"founder": 12}
    assert cfg["archetype_priority"]["cap"] == {"investor": 68}
    assert "Must be based in NYC" in cfg["hard_filters"]
    # intimate -> exclusive band
    assert cfg["thresholds"]["accept_fit_min"] == 78


# ── ask path ──────────────────────────────────────────────────────────────────

def test_ask_returns_question(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = ('{"action": "ask", "question": "Who is the room for?", '
               '"have": {}}')
    res = run_icp_turn([{"role": "user", "content": "I'm hosting an event"}],
                       client=_FakeClient(payload))
    assert res.complete is False
    assert res.question == "Who is the room for?"
    # Even a question-turn result can compile (to an empty/minimal config).
    assert isinstance(res.to_triage_config(), dict)


# ── fail-safe ─────────────────────────────────────────────────────────────────

def test_no_api_key_degrades(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = extract_icp("some event")
    assert res.complete is False
    assert "ANTHROPIC_API_KEY" in res.error
    assert res.to_triage_config() == _empty_cfg()


def test_client_error_is_swallowed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    res = extract_icp("event", client=_BoomClient())
    assert res.complete is False
    assert "RuntimeError" in res.error


def test_non_json_output_is_handled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    res = extract_icp("event", client=_FakeClient("I think you should..."))
    assert res.complete is False
    assert res.error


# ── normalization unit ────────────────────────────────────────────────────────

def test_normalize_coerces_and_filters():
    raw = {
        "role": "  founder  ",
        "capacity": "40",                       # str -> int
        "priority_archetypes": "founder",       # str -> [str]
        "deprioritize_archetypes": ["Investor", "investor"],  # dedup, lowercased? no
        "nice_to_have": ["", "  ", "real signal"],            # drop blanks
        "require_corroboration": "yes",         # str -> bool
        "weird": {"nested": 1},                  # dropped
    }
    out = _normalize_icp(raw)
    assert out["role"] == "founder"
    assert out["capacity"] == 40
    assert out["priority_archetypes"] == ["founder"]
    assert out["nice_to_have"] == ["real signal"]
    assert out["require_corroboration"] is True
    assert "weird" not in out


def _empty_cfg():
    from backend.triage.icp_compiler import compile_icp
    return compile_icp({})
