"""
Tests for the compose-result cache + prefetch hook.

Pins the contract that:
  - get_cached_compose() returns None for un-warmed entries
  - prefetch_compose_all() populates the cache for every prospect it sees
  - Cache entries expire after TTL
  - The prefetch is parallel (uses asyncio.to_thread so compose calls don't block)

Patches compose() to a no-network stub so tests don't hit Anthropic.
"""
from __future__ import annotations
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.agents import outreach


@pytest.fixture(autouse=True)
def clean_cache():
    outreach.reset_compose_cache()
    yield
    outreach.reset_compose_cache()


def _prospect(id, name="X"):
    return SimpleNamespace(
        id=id, name=name, role="Engineer", company="Co", seniority="Staff+",
        side="Builds", works_on="ml-platform", offers="depth", seeks="role",
        headline="", linkedin_url=f"https://linkedin.com/in/{name.lower()}",
    )


def _event(id=1):
    return SimpleNamespace(
        id=id, role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )


# ── basic cache contract ────────────────────────────────────────────────

def test_get_cached_compose_returns_none_for_unknown_key():
    assert outreach.get_cached_compose(prospect_id=999, event_id=999) is None


def test_store_then_read_roundtrip():
    msg = outreach.Message(note="n", message="m")
    outreach._store_compose(prospect_id=42, event_id=7, msg=msg)
    cached = outreach.get_cached_compose(prospect_id=42, event_id=7)
    assert cached is msg


def test_cached_entry_expires_after_ttl():
    msg = outreach.Message(note="n", message="m")
    # Put a stale entry directly into the cache
    outreach._COMPOSE_CACHE[(1, 1)] = (time.time() - outreach._COMPOSE_CACHE_TTL_S - 1, msg)
    assert outreach.get_cached_compose(prospect_id=1, event_id=1) is None
    # And the expired entry got evicted on access
    assert (1, 1) not in outreach._COMPOSE_CACHE


# ── prefetch behavior ──────────────────────────────────────────────────

def test_prefetch_populates_cache_for_every_prospect():
    prospects = [_prospect(i, name=f"P{i}") for i in range(5)]
    event = _event()
    fake_msg = outreach.Message(note="note", message="msg")

    with patch.object(outreach, "compose", return_value=fake_msg) as compose_mock:
        asyncio.run(outreach.prefetch_compose_all(prospects, event))

    assert compose_mock.call_count == 5
    for p in prospects:
        assert outreach.get_cached_compose(p.id, event.id) is fake_msg


def test_prefetch_swallows_individual_compose_failures():
    """One bad prospect (compose throws) shouldn't poison the whole batch :
    the rest should still land in cache, preview falls back live for the
    failed one."""
    prospects = [_prospect(i) for i in range(3)]
    event = _event()

    def flaky(p, *args, **kwargs):
        if p.id == 1:
            raise RuntimeError("simulated failure")
        return outreach.Message(note="ok", message="ok")

    with patch.object(outreach, "compose", side_effect=flaky):
        asyncio.run(outreach.prefetch_compose_all(prospects, event))

    assert outreach.get_cached_compose(0, event.id) is not None
    assert outreach.get_cached_compose(1, event.id) is None  # the failed one
    assert outreach.get_cached_compose(2, event.id) is not None


def test_prefetch_empty_list_is_noop():
    """Edge case : prospecting returned 0 candidates. Don't crash, don't hit
    the network, just return."""
    with patch.object(outreach, "compose") as compose_mock:
        asyncio.run(outreach.prefetch_compose_all([], _event()))
    compose_mock.assert_not_called()


def test_prefetch_runs_compose_in_parallel():
    """The whole point of prefetch is parallelism. Verify the batch finishes
    in roughly the time of one call, not N × one call. We simulate a 200ms
    compose : 10 prospects should complete in well under 1s if parallel,
    >2s if serialized."""
    prospects = [_prospect(i) for i in range(10)]
    event = _event()

    def slow_compose(p, *args, **kwargs):
        time.sleep(0.2)
        return outreach.Message(note="n", message="m")

    with patch.object(outreach, "compose", side_effect=slow_compose):
        t0 = time.time()
        asyncio.run(outreach.prefetch_compose_all(prospects, event))
        elapsed = time.time() - t0

    # Should be ~0.2s (parallel) not ~2.0s (serial). Generous threshold.
    assert elapsed < 1.0, f"prefetch took {elapsed:.2f}s : not parallel"
