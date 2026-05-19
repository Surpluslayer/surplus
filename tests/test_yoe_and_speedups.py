"""
Tests for the intake additions (Enterprise stage + YOE chips) and the
prospecting speedups (judge skip on single source, lower default timeouts).

Direct function tests : no TestClient (avoids the 3.9 / `str | None`
collection issue with schemas.py).
"""
from __future__ import annotations
import os
import pytest
from types import SimpleNamespace

from backend.agents import prospector
from backend.agents.exa import _build_query


# ── _build_query: YOE clause + Enterprise routing ──────────────────────

def test_query_does_not_include_yoe_clause():
    """YOE was added to the query in PR #46 but reverted : LinkedIn page
    text rarely contains literal "6-10 years experience", so the clause
    over-constrained the search and surfaced wrong people. YOE is still
    stored on Event for display + downstream use; just not in the query."""
    q = _build_query("linkedin", {
        "role": "ML engineers",
        "seniority": ["Senior"],
        "co_stage": ["Seed"],
        "yoe": ["6-10"],
        "city": "San Francisco",
    })
    assert "years experience" not in q


def test_query_routes_enterprise_to_companies_not_startups():
    """'enterprise startups' is wrong : Enterprise is its own track."""
    q = _build_query("linkedin", {
        "role": "engineers", "seniority": ["Senior"],
        "co_stage": ["Enterprise"],
    })
    assert "enterprise companies" in q
    assert "enterprise startups" not in q


def test_query_handles_mixed_startup_and_enterprise():
    q = _build_query("linkedin", {
        "role": "engineers", "seniority": ["Senior"],
        "co_stage": ["Seed", "Enterprise"],
    })
    assert "seed startups" in q
    assert "enterprise companies" in q


# ── Judge runs unconditionally (single source skip was reverted) ────────

def test_single_source_still_runs_judge(monkeypatch):
    """The single-source judge skip was reverted because LinkedIn alone
    surfaces wrong-person matches that the ICP gatekeeper would have
    caught. Trades ~4s of latency for search quality."""
    import asyncio
    from backend.agents import llm

    judge_calls: list = []
    monkeypatch.setattr(llm, "llm_available", lambda: True)

    async def _fake_judge_all(out, icp):
        judge_calls.append(True)
        return out
    monkeypatch.setattr(prospector, "_judge_all", _fake_judge_all)

    class _FakeAdapter:
        key = "linkedin"
        async def fetch(self, icp):
            return [{
                "identity": "x", "name": "X",
                "linkedin_url": "https://www.linkedin.com/in/x",
                "source": "linkedin",
            }]

    asyncio.run(prospector.prospect(
        {"role": "engineers", "seniority": "Senior", "city": "SF"},
        adapters=[_FakeAdapter()],
        force_fresh=True,
    ))
    assert judge_calls == [True], "Judge should run even for single-source"


def test_multi_source_still_runs_judge(monkeypatch):
    import asyncio
    from backend.agents import llm

    judge_calls: list = []
    monkeypatch.setattr(llm, "llm_available", lambda: True)

    async def _fake_judge_all(out, icp):
        judge_calls.append(True)
        return out
    monkeypatch.setattr(prospector, "_judge_all", _fake_judge_all)

    class _FakeAdapter:
        def __init__(self, k): self.key = k
        async def fetch(self, icp):
            return [{
                "identity": f"x-{self.key}",
                "name": f"X{self.key}",
                "linkedin_url": f"https://www.linkedin.com/in/x-{self.key}",
                "source": self.key,
            }]

    asyncio.run(prospector.prospect(
        {"role": "engineers", "seniority": "Senior", "city": "SF"},
        adapters=[_FakeAdapter("linkedin"), _FakeAdapter("github")],
        force_fresh=True,
    ))
    assert judge_calls == [True], "Judge should have run for multi-source"


# ── Defaults that the speedup PR changed ───────────────────────────────

def test_adapter_timeout_default_is_ten_seconds():
    """Was 30s when X was broken and Anthropic web_search was the slow path.
    Now Exa is fast everywhere, so 10s is plenty."""
    os.environ.pop("PROSPECTING_ADAPTER_TIMEOUT", None)
    assert prospector._adapter_timeout() == 10.0


def test_judge_timeout_default_is_six_seconds():
    """Restored to 6s after the 4s default was too aggressive : Haiku
    occasionally needs the extra room to finish the batch, and a timeout
    silently bypasses the ICP gate."""
    os.environ.pop("PROSPECTING_JUDGE_TIMEOUT", None)
    assert prospector._judge_timeout() == 6.0
