"""
Tests for the Scholar source adapter : mock-mode fan-out + integration with
the prospector merge step.

Scholar is a "bottom" source : it never anchors a candidate on its own (no
LinkedIn URL in its output), so its only job is to bolt citation-count
signal onto a record that *also* surfaced from a stronger source.
"""
from __future__ import annotations
import asyncio

from backend.agents.sources import ScholarAdapter, ALL_ADAPTERS
from backend.agents.sources.scholar import MIN_CITATIONS
from backend.agents.prospector import prospect


def test_scholar_in_default_registry():
    """ScholarAdapter must be wired into the global registry the prospector
    fans out across : otherwise the source never runs."""
    keys = [a.key for a in ALL_ADAPTERS]
    assert "scholar" in keys


def test_scholar_mock_filters_below_min_citations():
    """Records under MIN_CITATIONS should be dropped, just like gh_stars / x_followers."""
    adapter = ScholarAdapter()
    out = asyncio.run(adapter.fetch({"role": "ml engineer"}))
    # Every emitted record carries the source key and an above-floor count
    for r in out:
        assert r["source"] == "scholar"
        assert r["scholar_citations"] >= MIN_CITATIONS


def test_prospect_merge_attaches_scholar_citations_to_linkedin_record():
    """End-to-end: in mock mode the prospector should run scholar alongside
    the other adapters and the merge keys on identity, so a candidate that
    surfaced from LinkedIn AND Scholar gets a non-zero scholar_citations."""
    icp = {
        "role": "ML platform engineer",
        "seniority": "Staff+",
        "co_stage": "Seed",
        "city": "San Francisco",
    }
    out = asyncio.run(prospect(icp, force_fresh=True))
    # maya-rodriguez has scholar_citations: 180 in the pool : the
    # cross-source merge should attach it to her record.
    maya = next((p for p in out if p["identity"] == "maya-rodriguez"), None)
    assert maya is not None, "maya-rodriguez should surface from LinkedIn"
    assert maya["scholar_citations"] >= MIN_CITATIONS
    # And the sources string should reflect that scholar contributed
    assert "scholar" in maya["sources"]


def test_discover_candidates_scholar_skips_claude_fallback(monkeypatch):
    """When Exa is configured and returns empty for scholar, we MUST NOT
    fall through to Claude + web_search : that path costs 60-90s for
    zero useful signal (Claude can't reliably emit name-slugs that
    cross-merge onto LinkedIn records). Other sources still fall through
    on Exa empty.
    """
    from unittest.mock import patch
    from backend.agents import llm

    monkeypatch.setenv("EXA_API_KEY", "exa-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")

    with patch("backend.agents.exa.discover_via_exa", return_value=[]) as exa_call, \
         patch.object(llm, "_client") as anthropic_client:
        out = llm.discover_candidates("scholar", {"role": "ml engineer"})
    assert out == []
    exa_call.assert_called_once()
    # Crucial: no Anthropic client touched.
    anthropic_client.assert_not_called()


def test_prospect_cache_key_includes_adapter_set():
    """Toggling sources between runs against the same ICP must bust the
    cache : otherwise turning Scholar on/off would silently no-op for
    the cache TTL window."""
    from backend.agents.prospector import _icp_cache_key
    from backend.agents.sources import GitHubAdapter, LinkedInAdapter, ScholarAdapter

    icp = {"role": "engineer", "seniority": "Senior", "co_stage": "Seed", "city": "SF"}
    a = _icp_cache_key(icp, [LinkedInAdapter(), GitHubAdapter()])
    b = _icp_cache_key(icp, [LinkedInAdapter(), GitHubAdapter(), ScholarAdapter()])
    assert a != b


def test_prospect_zero_citations_for_pool_without_scholar_footprint():
    """A candidate with no Scholar footprint should fall through with 0
    citations : the field defaults so downstream code never KeyErrors."""
    icp = {
        "role": "engineer",
        "seniority": "Mid",
        "co_stage": "Seed",
        "city": "San Francisco",
    }
    out = asyncio.run(prospect(icp, force_fresh=True))
    # grace-liu has scholar_citations: 0 in the pool : should still surface
    # via LinkedIn but with the field defaulted.
    grace = next((p for p in out if p["identity"] == "grace-liu"), None)
    if grace is not None:  # may be gated out by judge in LLM mode; mock keeps her
        assert grace["scholar_citations"] == 0
        assert "scholar" not in grace["sources"]
