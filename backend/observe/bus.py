"""backend/observe/bus.py : a per-account ring buffer so work happening in
the PRODUCT shows up in the Observe log.

Everything else in backend/observe/ is pull-based: Observe calls the real
code itself and reports what it did. That cannot work for the ask bar or a
Draft tap, because those run in their own request (routes/book.py) with
their own SSE stream to the product UI. Observe can either re-run the ask
-- spending a second set of model calls and showing results the user never
saw -- or it can listen. It listens.

routes/book.py publishes milestones here as they happen; Observe's
/stream/activity tails the buffer. Publishing is fire-and-forget and fully
guarded: an Observe problem must never break a real ask or draft.

LIMITATION, stated because it is easy to mistake this for durable
infrastructure: this is an in-process buffer. With WEB_CONCURRENCY > 1 an
ask served by worker A is invisible to an Observe stream held open by
worker B, and nothing survives a restart. That is honest for a debugger
that shows "what is happening right now" on a single-worker deploy (the
default here), and it is the reason this is a ring buffer in memory rather
than a table: a durable audit log is a different feature with different
retention and privacy questions, and pretending this is one would be worse
than the limitation.
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

_UTC = timezone.utc

# Per-account rolling window. Small on purpose: this feeds a live tail, not
# a history view.
_MAX_PER_ACCOUNT = 400

_lock = threading.Lock()
_buffers: dict = defaultdict(lambda: deque(maxlen=_MAX_PER_ACCOUNT))
_seq = 0


def publish(account_id: int, level: str, src: str, msg: str, **extra) -> None:
    """Record one product-side event. Never raises -- a logging failure must
    not affect the ask/draft that produced it."""
    global _seq
    try:
        with _lock:
            _seq += 1
            _buffers[account_id].append({
                "seq": _seq,
                "ts": datetime.now(_UTC).isoformat(),
                "level": level, "src": src, "msg": msg, **extra,
            })
    except Exception:  # noqa: BLE001
        pass


def since(account_id: int, after_seq: int = 0) -> list:
    """Events for this account newer than `after_seq`, oldest first."""
    try:
        with _lock:
            return [e for e in _buffers.get(account_id, ()) if e["seq"] > after_seq]
    except Exception:  # noqa: BLE001
        return []


def latest_seq() -> int:
    with _lock:
        return _seq
