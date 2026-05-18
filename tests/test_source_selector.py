"""
Tests for the source-selector :
  - EventCreate validation (mandatory LinkedIn, max-3 cap, allowed keys)
  - EventOut round-trip
  - pipeline._adapters_for_event picks only the operator's selected sources
"""
from __future__ import annotations
import pytest
from types import SimpleNamespace

from backend import schemas
from backend.pipeline import _adapters_for_event
from backend.agents.sources import ALL_ADAPTERS


# ---- EventCreate validation -------------------------------------------------

def test_default_sources_include_linkedin():
    """Bare POST /events {} must surface a sane default. LinkedIn always in."""
    ev = schemas.EventCreate()
    assert "linkedin" in ev.enabled_sources
    assert len(ev.enabled_sources) <= schemas.MAX_SOURCES


def test_explicit_sources_accepted():
    ev = schemas.EventCreate(enabled_sources=["linkedin", "github", "scholar"])
    assert ev.enabled_sources == ["linkedin", "github", "scholar"]


def test_sources_validation_requires_linkedin():
    with pytest.raises(ValueError, match="linkedin"):
        schemas.EventCreate(enabled_sources=["github", "x"])


def test_sources_validation_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown source"):
        schemas.EventCreate(enabled_sources=["linkedin", "facebook"])


def test_sources_validation_caps_at_max():
    with pytest.raises(ValueError, match="at most"):
        schemas.EventCreate(
            enabled_sources=["linkedin", "github", "x", "scholar"]
        )


def test_sources_validation_dedupes_and_lowercases():
    ev = schemas.EventCreate(
        enabled_sources=["LinkedIn", "github", "linkedin", "GITHUB"]
    )
    assert ev.enabled_sources == ["linkedin", "github"]


def test_sources_validation_strips_whitespace_and_empties():
    ev = schemas.EventCreate(enabled_sources=["  linkedin ", "", "github"])
    assert ev.enabled_sources == ["linkedin", "github"]


# ---- EventOut round-trip ----------------------------------------------------

def test_event_out_exposes_enabled_sources():
    """EventOut.of() reads the CSV column back into a list."""
    from datetime import datetime, timezone
    ev = SimpleNamespace(
        id=1, role="x", seniority="Senior", co_stage="Seed",
        headcount=10, format="Mixer", city="SF", goal="Hiring pipeline",
        budget=1000, enabled_sources="linkedin,scholar",
        threshold=0, created_at=datetime.now(timezone.utc),
    )
    out = schemas.EventOut.of(ev)
    assert out.enabled_sources == ["linkedin", "scholar"]


def test_event_out_handles_legacy_null_sources():
    """Pre-migration rows have NULL/empty enabled_sources : fall back to a
    full registry default so behavior matches the pre-feature world."""
    from datetime import datetime, timezone
    ev = SimpleNamespace(
        id=1, role="x", seniority="Senior", co_stage="Seed",
        headcount=10, format="Mixer", city="SF", goal="Hiring pipeline",
        budget=1000, enabled_sources="",
        threshold=0, created_at=datetime.now(timezone.utc),
    )
    out = schemas.EventOut.of(ev)
    assert "linkedin" in out.enabled_sources


# ---- pipeline._adapters_for_event ------------------------------------------

def test_adapters_for_event_filters_by_csv():
    ev = SimpleNamespace(enabled_sources="linkedin,scholar")
    keys = [a.key for a in _adapters_for_event(ev)]
    assert set(keys) == {"linkedin", "scholar"}


def test_adapters_for_event_force_includes_linkedin():
    """Defense-in-depth : even if a malformed row has only github, we
    still anchor on LinkedIn so the no-linkedin-url filter doesn't drop
    everything downstream."""
    ev = SimpleNamespace(enabled_sources="github,x")
    keys = {a.key for a in _adapters_for_event(ev)}
    assert "linkedin" in keys
    assert "github" in keys
    assert "x" in keys


def test_adapters_for_event_empty_falls_back_to_full_registry():
    """Empty / null on a legacy row → run every adapter (preserves
    pre-migration behavior)."""
    ev = SimpleNamespace(enabled_sources="")
    keys = {a.key for a in _adapters_for_event(ev)}
    assert keys == {a.key for a in ALL_ADAPTERS}


def test_adapters_for_event_ignores_unknown_keys():
    """A typo'd key just gets skipped : linkedin is still forced on."""
    ev = SimpleNamespace(enabled_sources="linkedin,facebook")
    keys = {a.key for a in _adapters_for_event(ev)}
    assert keys == {"linkedin"}
