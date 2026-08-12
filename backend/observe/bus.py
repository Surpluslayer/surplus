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
import queue
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
    """A session THIS module created, on whatever bind the app (or a test) is
    configured for -- so closing it is always safe.

    Deliberately not just `factory()`. A factory is not guaranteed to hand
    back a NEW session: the app's SessionLocal does, but code that needs to
    redirect the database (tests do this) may return a session the CALLER is
    also using. Closing that detaches the caller's objects mid-request, which
    is not a hypothetical -- it turned a live ask into
    `DetachedInstanceError: Instance <User> is not bound to a Session` the
    moment this module started closing what the factory gave it.

    So the factory result is used only to read the bind, and the session we
    actually work on is one we opened ourselves. The probe holds no
    connection (a Session opens one lazily, on first use, and get_bind() is
    not a use), so leaving it to the garbage collector costs nothing.
    """
    from sqlalchemy.orm import Session as _Session

    factory = _session_factory
    if factory is None:
        from ..db import SessionLocal
        factory = SessionLocal
    return _Session(bind=factory().get_bind())


def _ttl_minutes() -> int:
    try:
        return max(1, int((os.environ.get("OBSERVE_ACTIVITY_TTL_MIN") or "30").strip()))
    except ValueError:
        return 30


# ── the write path: OFF the request thread ───────────────────────────────
#
# publish() used to open a session, INSERT and COMMIT inline. Measured on a
# real ask: consecutive publishes with NO work between them landed ~0.55s
# apart, because each one queues for a pooled connection against 6 concurrent
# drafting threads and 8 total connections. At ~19 lines per ask that is
# roughly ten seconds added to a request the user is waiting on -- by the
# instrumentation, whose entire contract is that it never affects the ask.
#
# So the request thread now only enqueues, and one background writer drains
# the queue in batches. A single writer also keeps insertion order equal to
# publish order, so the autoincrement id stays a valid stream cursor.
_QUEUE: "queue.Queue" = queue.Queue(maxsize=4000)
_writer: "threading.Thread | None" = None
_dropped = 0
_BATCH = 200
# Enqueued but not yet committed. An EMPTY QUEUE IS NOT A FINISHED WRITE --
# the writer holds a batch in hand while it commits, so flush() has to wait on
# this counter rather than on Queue.empty(). Waiting on the queue alone lost
# 17 of 19 lines in a test that asserted every line lands.
_pending = 0


def _ensure_writer() -> None:
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _lock:
        if _writer is not None and _writer.is_alive():
            return
        t = threading.Thread(target=_drain_forever, name="observe-bus-writer",
                             daemon=True)
        t.start()
        _writer = t


def _drain_forever() -> None:
    while True:
        try:
            first = _QUEUE.get()
            batch = [first]
            while len(batch) < _BATCH:
                try:
                    batch.append(_QUEUE.get_nowait())
                except queue.Empty:
                    break
            _write_batch(batch, tracked=True)
        except Exception:  # noqa: BLE001 -- the writer must never die
            pass


def _write_batch(batch: list, *, tracked: bool = False) -> None:
    """One session and one commit for the whole batch."""
    global _pruned_once, _pending
    try:
        from .. import models
        db = _make_session()
        try:
            db.add_all([models.ObserveActivity(**row) for row in batch])
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
    finally:
        if tracked:
            with _lock:
                _pending -= len(batch)


def _row(account_id: int, level: str, src: str, msg: str, extra: dict) -> dict:
    payload = None
    if extra:
        try:
            payload = json.dumps(extra, default=str)
        except Exception:  # noqa: BLE001 -- a bad extra must not lose the line
            payload = None
    # Stamped HERE, not at insert time: the timestamp has to say when the work
    # happened, not when the writer got around to recording it.
    return {"user_id": int(account_id), "level": str(level)[:10],
            "src": str(src)[:160], "msg": str(msg), "extra_json": payload,
            "created_at": datetime.now(_UTC)}


def flush(timeout: float = 2.0) -> bool:
    """Block until every enqueued line has been COMMITTED. For tests and
    shutdown, never the request path -- that is the whole point of the queue.

    Waits on the in-flight counter, not on Queue.empty(): the writer pops a
    batch before committing it, so the queue goes empty while the rows are
    still unwritten."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        with _lock:
            if _pending == 0:
                return True
        _time.sleep(0.005)
    return False


def dropped_count() -> int:
    """Lines discarded because the queue was full. Surfaced rather than
    hidden: a silently lossy log is worse than one that admits a gap."""
    return _dropped


def publish(account_id: int, level: str, src: str, msg: str, **extra) -> None:
    """Record one product-side event. Never raises and never blocks -- a
    logging failure, or a slow database, must not affect the ask or draft
    that produced it.

    Writes are batched onto a background thread (see above). When a session
    factory has been injected (tests), the write happens inline instead so
    assertions do not race the writer.
    """
    global _dropped, _pending
    try:
        row = _row(account_id, level, src, msg, extra)
        if _session_factory is not None:
            _write_batch([row])       # tests: deterministic, no writer thread
            return
        _ensure_writer()
        with _lock:
            _pending += 1
        try:
            _QUEUE.put_nowait(row)
        except queue.Full:
            with _lock:
                _pending -= 1
                _dropped += 1
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
