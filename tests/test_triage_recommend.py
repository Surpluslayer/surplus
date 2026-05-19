"""
Tests for backend/triage/recommend.py : the deterministic fit + confidence
-> recommendation logic.

Keeps the cutoffs honest as code evolves : if someone tweaks the bands,
they'll know which behaviors changed.
"""
from __future__ import annotations
from types import SimpleNamespace

import pytest

from backend.triage import recommend


def _app(**fields):
    """Build an applicant-shaped object. Defaults are empty so tests can
    set only the fields they care about."""
    defaults = dict(
        name="", email=None, role=None, company=None,
        website=None, linkedin_url=None, raw_application_data="{}",
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


# ── confidence_floor ──────────────────────────────────────────────────

def test_confidence_floor_zero_for_empty():
    assert recommend.compute_confidence_floor(_app()) == 0


def test_confidence_floor_rises_with_each_field():
    """Each canonical field contributes; LinkedIn + website weigh more."""
    sparse = _app(name="Maya", email="m@x.com")
    rich = _app(name="Maya", email="m@x.com", role="Staff Eng",
                company="Acme", linkedin_url="https://linkedin.com/in/maya",
                website="https://acme.com")
    assert recommend.compute_confidence_floor(rich) > recommend.compute_confidence_floor(sparse)


def test_confidence_floor_caps_at_100():
    very_rich = _app(
        name="x", email="x", role="x", company="x",
        linkedin_url="x", website="x",
        raw_application_data="x" * 5000,
    )
    assert recommend.compute_confidence_floor(very_rich) <= 100


def test_confidence_floor_rewards_long_application_answers():
    short_ans = _app(name="x", raw_application_data='{"q": "a"}')
    long_ans = _app(name="x", raw_application_data='{"q": "' + "a" * 300 + '"}')
    assert recommend.compute_confidence_floor(long_ans) > \
           recommend.compute_confidence_floor(short_ans)


# ── fit_from_dimensions ────────────────────────────────────────────────

def test_fit_uses_weighted_sum():
    dims = {n: 100 for n in recommend.DEFAULT_WEIGHTS}
    assert recommend.fit_from_dimensions(dims) == 100
    dims = {n: 0 for n in recommend.DEFAULT_WEIGHTS}
    assert recommend.fit_from_dimensions(dims) == 0


def test_fit_treats_missing_dimensions_as_zero():
    """Partial output from a flaky LLM call shouldn't accidentally make a
    half-scored applicant look better than fully-scored ones."""
    dims = {"sponsor_fit": 100}
    fit = recommend.fit_from_dimensions(dims)
    # sponsor_fit weight is 0.25 in DEFAULT_WEIGHTS, so 25-ish.
    assert 20 <= fit <= 30


def test_fit_respects_custom_weights():
    """When the rubric supplies different weights, fit reflects them."""
    dims = {n: 50 for n in recommend.DEFAULT_WEIGHTS}
    dims["sponsor_fit"] = 100  # crank one dimension
    weight_heavy = {"sponsor_fit": 0.9, "event_fit": 0.1}
    weight_light = {"sponsor_fit": 0.1, "event_fit": 0.9}
    assert recommend.fit_from_dimensions(dims, weight_heavy) > \
           recommend.fit_from_dimensions(dims, weight_light)


# ── recommendation_from ───────────────────────────────────────────────

@pytest.mark.parametrize("fit,conf,expected", [
    (90, 80, "accept"),
    (75, 60, "accept"),
    (74, 80, "maybe"),            # just below accept fit, still hits maybe band
    (75, 59, "maybe"),            # just below accept confidence, still hits maybe band
    (60, 60, "maybe"),
    (55, 50, "maybe"),
    (54, 50, "needs_review"),     # below maybe fit
    (30, 90, "reject"),
    (39, 50, "reject"),           # below reject threshold
    (40, 30, "needs_review"),     # ambiguous mid-range, low confidence
])
def test_recommendation_buckets(fit, conf, expected):
    assert recommend.recommendation_from(fit, conf) == expected


# ── finalize : the integration ────────────────────────────────────────

def test_finalize_caps_confidence_by_floor():
    """LLM bullish + sparse data : final confidence stays low."""
    sparse = _app(name="x")  # very low data signal
    dims = {n: 100 for n in recommend.DEFAULT_WEIGHTS}
    out = recommend.finalize(sparse, dims, llm_confidence=95)
    assert out.confidence_score < 50  # floor is low → final low
    assert out.fit_score == 100


def test_finalize_llm_can_lower_confidence_below_floor():
    """Data-rich but LLM thinks signals are inconsistent : final low."""
    rich = _app(name="Maya", email="m@x.com", role="Staff",
                company="Acme", linkedin_url="https://linkedin.com/in/maya",
                website="https://acme.com",
                raw_application_data="x" * 500)
    dims = {n: 90 for n in recommend.DEFAULT_WEIGHTS}
    out = recommend.finalize(rich, dims, llm_confidence=20)
    assert out.confidence_score == 20  # LLM lowered it


def test_finalize_recommendation_is_consistent_with_inputs():
    dims = {n: 80 for n in recommend.DEFAULT_WEIGHTS}
    rich = _app(name="x", email="x", role="x", company="x",
                linkedin_url="x", website="x",
                raw_application_data="x" * 200)
    out = recommend.finalize(rich, dims, llm_confidence=80)
    assert out.recommendation == "accept"
    assert out.fit_score == 80
