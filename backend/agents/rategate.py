"""agents/rategate.py : one priority gate in front of every relationship-layer
Claude call.

The relationship layer is a pile of fan-outs ("do an AI thing for each of N
people" : score health, detect updates, draft, classify). They all draw on one
Anthropic key with a fixed per-minute budget, so any "across my whole book"
action can burst past the limit -> 429 -> backoff -> user-visible stall.

This gate makes that rare and controlled instead of random:

  * a TOTAL in-flight cap (CLAUDE_MAX_CONCURRENCY) so we self-throttle BELOW the
    key's real concurrency instead of letting the SDK discover the limit via 429s,
  * a smaller BACKGROUND sub-cap (CLAUDE_BG_CONCURRENCY), and
  * soft priority: a background call won't grab a slot while a FOREGROUND call is
    queued for one. User clicks win the race; sweeps wait behind them.

It does NOT replace the SDK's own retry; it shrinks how often that retry is
needed and keeps the pain off the user's request. Anything that calls Claude
without going through here can still burst -- so route relationship-layer calls
through gate().

Usage:
    with gate(background=False):      # foreground: /ask, clicked draft
        resp = client.messages.create(...)
    with gate(background=True):       # background: updates sweep, bulk scoring
        ...
"""
from __future__ import annotations

import os
import threading
import time

_TOTAL = max(1, int(os.environ.get("CLAUDE_MAX_CONCURRENCY", "5")))
_BG_CAP = max(1, min(_TOTAL, int(os.environ.get("CLAUDE_BG_CONCURRENCY", "2"))))

_sem = threading.BoundedSemaphore(_TOTAL)      # total concurrent Claude calls
_bg_sem = threading.BoundedSemaphore(_BG_CAP)  # background's smaller slice
_fg_waiting = 0                                 # foreground calls queued for a slot
_fg_lock = threading.Lock()
# How long a background call yields per poll while foreground is queued, and the
# ceiling on total yield so a stuck foreground counter can never hang the sweep.
_YIELD_TICK = 0.05
_YIELD_MAX_S = float(os.environ.get("CLAUDE_BG_YIELD_MAX_S", "30"))


class _Gate:
    __slots__ = ("background",)

    def __init__(self, background: bool):
        self.background = background

    def __enter__(self):
        global _fg_waiting
        if self.background:
            # Soft-yield: don't start NEW background work while a foreground call
            # is waiting for a slot (bounded so it can't wait forever).
            waited = 0.0
            while waited < _YIELD_MAX_S:
                with _fg_lock:
                    pending = _fg_waiting
                if pending == 0:
                    break
                time.sleep(_YIELD_TICK)
                waited += _YIELD_TICK
            _bg_sem.acquire()
            _sem.acquire()
        else:
            with _fg_lock:
                _fg_waiting += 1
            try:
                _sem.acquire()
            finally:
                with _fg_lock:
                    _fg_waiting -= 1
        return self

    def __exit__(self, *exc):
        _sem.release()
        if self.background:
            _bg_sem.release()
        return False


def gate(background: bool = False) -> _Gate:
    """Acquire a Claude-call slot. `background=True` uses the smaller sub-cap and
    yields to any queued foreground call."""
    return _Gate(background)
