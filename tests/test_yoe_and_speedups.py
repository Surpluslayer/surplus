"""
Tests for the Exa query builder (Enterprise stage + YOE chips).

Direct function tests : no TestClient (avoids the 3.9 / `str | None`
collection issue with schemas.py).

The prospector tests that used to live here were deleted with the events-side
prospecting pipeline (backend/agents/events/).
"""
from __future__ import annotations

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
