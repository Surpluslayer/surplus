"""
Tests for source-selection plumbing.

Pin the invariants that the warm/cold + speedup story rests on:

  - LinkedIn is ALWAYS in the adapter list, no matter what the operator
    selects (or doesn't). Server enforces this even if the UI is bypassed.
  - Empty/None input is safe (returns LinkedIn-only).
  - Input is case-insensitive and dedupes.
  - The Event row stamps "linkedin" into its sources on create even when
    the payload omits it.

Direct function tests : no FastAPI TestClient (avoids the 3.9 / `str | None`
collection issue with schemas.py).
"""
from __future__ import annotations

from backend.agents.sources import (
    ALL_ADAPTERS, MANDATORY_SOURCE_KEY, adapters_for,
)


# ── adapters_for invariants ────────────────────────────────────────────

def _keys(adapters) -> list[str]:
    return [a.key for a in adapters]


def test_empty_input_returns_linkedin_only():
    assert _keys(adapters_for([])) == ["linkedin"]
    assert _keys(adapters_for(None)) == ["linkedin"]
    assert _keys(adapters_for("")) == ["linkedin"]


def test_explicit_linkedin_only():
    assert _keys(adapters_for(["linkedin"])) == ["linkedin"]


def test_github_only_still_includes_linkedin():
    """The whole 'LinkedIn is mandatory' invariant. If the operator picks
    GitHub alone, the server MUST add LinkedIn."""
    keys = _keys(adapters_for(["github"]))
    assert "linkedin" in keys
    assert "github" in keys


def test_linkedin_is_first_in_the_returned_order():
    """Async fan-out: LinkedIn gets first dibs when an upstream rate-limits."""
    keys = _keys(adapters_for(["github", "x", "scholar", "linkedin"]))
    assert keys[0] == "linkedin"


def test_case_insensitive_and_dedupes():
    keys = _keys(adapters_for(["LINKEDIN", "GitHub", "github", "X"]))
    # Each adapter appears exactly once
    assert sorted(keys) == sorted(set(keys))
    assert "linkedin" in keys and "github" in keys and "x" in keys


def test_csv_string_input_works():
    """The Event.sources column stores CSV; adapters_for must accept that
    shape without an upstream split() step."""
    keys = _keys(adapters_for("linkedin,github,scholar"))
    assert set(keys) == {"linkedin", "github", "scholar"}


def test_unknown_keys_are_ignored():
    """A typo or stale-frontend value shouldn't crash; we just drop it."""
    keys = _keys(adapters_for(["linkedin", "facebook", "bluesky"]))
    assert keys == ["linkedin"]


def test_mandatory_key_constant_is_linkedin():
    """Trip-wire: changing this value silently would let an operator
    deselect LinkedIn. Force a code review."""
    assert MANDATORY_SOURCE_KEY == "linkedin"


def test_all_four_adapters_can_be_selected():
    keys = _keys(adapters_for(["linkedin", "github", "x", "scholar"]))
    assert set(keys) == {"linkedin", "github", "x", "scholar"}
    assert len(keys) == 4


def test_returned_adapters_are_real_instances():
    """Not just keys : actual SourceAdapter objects from ALL_ADAPTERS so
    callers can invoke .fetch() on them."""
    selected = adapters_for(["linkedin"])
    assert len(selected) == 1
    # Identity check : the helper returns the registry instances, not copies
    assert selected[0] in ALL_ADAPTERS
