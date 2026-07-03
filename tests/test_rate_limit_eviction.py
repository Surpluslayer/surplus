"""
Tests for the per-IP rate limiter's memory-leak guard.

Behind Cloudflare every distinct client IP mints a bucket keyed by
"<tag>:<ip>". Without eviction those keys accumulate forever (unbounded slow
memory growth). These pin that:
  - a bucket is evicted once its sliding window fully expires
  - the limiter's rate-limiting behavior is otherwise unchanged
"""
from __future__ import annotations
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import rate_limit


def _req(ip: str):
    """Minimal Request stand-in : the limiter only reads headers + client."""
    return SimpleNamespace(
        headers={"cf-connecting-ip": ip},
        client=SimpleNamespace(host=ip),
    )


@pytest.fixture(autouse=True)
def _clear_windows():
    rate_limit._WINDOWS.clear()
    yield
    rate_limit._WINDOWS.clear()


def test_key_evicted_once_window_fully_expires():
    dep = rate_limit.per_ip_rate_limit(limit=3, window_s=1, tag="t")
    ip = "203.0.113.7"
    dep(_req(ip))
    assert f"t:{ip}" in rate_limit._WINDOWS  # bucket now exists

    # Age the single timestamp past the window so the next call finds it empty.
    time.sleep(1.1)
    dep(_req(ip))  # window expired -> old bucket evicted, fresh one created
    # Still present (this request re-created it), but it holds exactly one entry,
    # proving the stale timestamps were dropped, not accumulated.
    assert len(rate_limit._WINDOWS[f"t:{ip}"]) == 1


def test_stale_key_from_never_returning_ip_is_swept():
    dep = rate_limit.per_ip_rate_limit(limit=3, window_s=1, tag="t")
    # A one-shot IP that never comes back.
    dep(_req("198.51.100.1"))
    assert "t:198.51.100.1" in rate_limit._WINDOWS

    # Force its lone timestamp far past the widest window.
    now = time.monotonic()
    rate_limit._WINDOWS["t:198.51.100.1"].clear()
    rate_limit._WINDOWS["t:198.51.100.1"].append(now - rate_limit._MAX_WINDOW_S - 10)

    # A DIFFERENT IP whose own bucket has expired : its call finds an empty
    # bucket, which fires the piggybacked _sweep_stale that evicts the
    # abandoned first key.
    dep(_req("198.51.100.2"))
    time.sleep(1.1)  # expire 198.51.100.2's own window
    dep(_req("198.51.100.2"))  # empty-bucket path -> _sweep_stale runs
    assert "t:198.51.100.1" not in rate_limit._WINDOWS


def test_rate_limiting_behavior_unchanged():
    dep = rate_limit.per_ip_rate_limit(limit=2, window_s=60, tag="t")
    ip = "192.0.2.9"
    dep(_req(ip))
    dep(_req(ip))
    with pytest.raises(HTTPException) as ei:
        dep(_req(ip))
    assert ei.value.status_code == 429
