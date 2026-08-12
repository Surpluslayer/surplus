"""backend/observe/bus.py : product-side activity for the Observe log.

Everything else in backend/observe/ is pull-based: Observe calls the real
code itself and reports what it did. That cannot work for the ask bar or a
Draft tap, because those run in their own request (routes/book.py) with
their own SSE stream to the product UI. Observe can either re-run the ask
-- spending a second set of model calls and showing results the user never
saw -- or it can listen. It listens.

routes/book.py publishes milestones here as they happen; Observe's
/stream/activity tails them. Publishing is fire-and-forget and fully
guarded: an Observe problem must never break a real ask or draft.

WHY THIS IS A TABLE AND NOT A RING BUFFER
-----------------------------------------
It was an in-process deque, with the limitation written down: "with
WEB_CONCURRENCY > 1 an ask served by worker A is invisible to a stream held
open by worker B." That limitation turned out to be the whole feature. The
ask and the Observe stream are two separate HTTP requests, so on any
multi-worker deploy they land on different processes routinely rather than
occasionally -- and the ask-bar narration simply never appeared. A
documented limitation that makes the feature not work is a bug.

Rows are keyed by an autoincrement id that doubles as the stream cursor:
monotonic per database, so a reader resumes with "id > last_seen" no matter
which worker serves the next poll.

RETENTION -- this is a live tail, not an audit log
--------------------------------------------------
Lines carry query text and contact names. Making them durable would quietly
turn a debugger into a permanent record of who a user asked about, which is
a different feature with different privacy and retention questions. So
writes prune anything older than OBSERVE_ACTIVITY_TTL_MIN (default 30), and
the pruning is part of the write path rather than a sweep that might never
run.
"""
from __future__ import annotations

import json
import os
import random
import threading
from datetime import datetime, timedelta, timezone

_UTC = timezone.utc

# Pruning on every insert would double the write cost for no benefit; the
# window is 30 minutes and a few stale rows are harmless. Prune on ~1 write
# in _PRUNE_EVERY, plus always on the first write of a process.
_PRUNE_EVERY = 25
_pruned_once = False
_lock = threading.Lock()

# Session factory. Defaults to the app's SessionLocal; tests point it at their
# own engine. An explicit seam rather than monkeypatching a module attribute,
# because publish() deliberately does NOT accept a caller's session (it must
# not join a transaction that may roll back) and so has no other way in.
_session_factory = None


def use_session_factory(factory) -> None:
    """Point the bus at a different sessionmaker. Pass None to restore the
    default. Intended for tests."""
    global _session_factory, _pruned_once
    _session_factory = factory
    _pruned_once = False


def _make_session():
    if _session_factory is not None:
        return _session_factory()
    from ..db import SessionLocal
    return SessionLocal()


def _ttl_minutes() -> int:
    try:
        return max(1, int((os.environ.get("OBSERVE_ACTIVITY_TTL_MIN") or "30").strip()))
    except ValueError:
        return 30


def publish(account_id: int, level: str, src: str, msg: str, **extra) -> None:
    """Record one product-side event. Never raises -- a logging failure must
    not affect the ask or draft that produced it.

    Uses its own short-lived session rather than the caller's: the caller is
    mid-request and may roll back, and an Observe write must never be part of
    that transaction (nor leave the caller's session dirty).
    """
    global _pruned_once
    try:
        from .. import models

        payload = None
        if extra:
            try:
                payload = json.dumps(extra, default=str)
            except Exception:  # noqa: BLE001 -- a bad extra must not lose the line
                payload = None

        db = _make_session()
        try:
            db.add(models.ObserveActivity(
                user_id=int(account_id),
                level=str(level)[:10],
                src=str(src)[:160],
                msg=str(msg),
                extra_json=payload,
            ))
            db.commit()

            with _lock:
                should_prune = not _pruned_once
                _pruned_once = True
            if should_prune or random.randrange(_PRUNE_EVERY) == 0:
                cutoff = datetime.now(_UTC) - timedelta(minutes=_ttl_minutes())
                db.query(models.ObserveActivity).filter(
                    models.ObserveActivity.created_at < cutoff).delete(
                        synchronize_session=False)
                db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


def since(account_id: int, after_seq: int = 0, db=None) -> list:
    """Events for this account newer than `after_seq`, oldest first.

    `db` lets a caller that already holds a session reuse it (the SSE poller
    opens and closes one per tick so it never pins a pool connection).
    """
    own = db is None
    try:
        from .. import models

        if own:
            db = _make_session()
        try:
            rows = (db.query(models.ObserveActivity)
                      .filter(models.ObserveActivity.user_id == int(account_id),
                              models.ObserveActivity.id > int(after_seq))
                      .order_by(models.ObserveActivity.id)
                      .limit(500)
                      .all())
            out = []
            for r in rows:
                ev = {
                    "seq": r.id,
                    "ts": (r.created_at.replace(tzinfo=_UTC).isoformat()
                           if r.created_at is not None
                           and r.created_at.tzinfo is None
                           else (r.created_at.isoformat() if r.created_at else None)),
                    "level": r.level, "src": r.src, "msg": r.msg,
                }
                if r.extra_json:
                    try:
                        ev.update(json.loads(r.extra_json))
                    except Exception:  # noqa: BLE001
                        pass
                out.append(ev)
            return out
        finally:
            if own:
                db.close()
    except Exception:  # noqa: BLE001
        return []


def latest_seq(db=None) -> int:
    """Highest id in the table, i.e. the cursor a new reader should start from
    so it sees only NEW activity. 0 when the table is empty or unreadable."""
    own = db is None
    try:
        from sqlalchemy import func

        from .. import models

        if own:
            db = _make_session()
        try:
            return int(db.query(func.max(models.ObserveActivity.id)).scalar() or 0)
        finally:
            if own:
                db.close()
    except Exception:  # noqa: BLE001
        return 0
