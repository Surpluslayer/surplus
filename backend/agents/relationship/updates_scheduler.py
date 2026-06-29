"""agents/updates_scheduler.py : in-process scheduler for the updates sweep.

Replaces the flaky external GitHub-Actions cron with a background thread that
lives INSIDE the product. No external secrets, no missed/delayed scheduled runs,
and it's visible to the app (status via /api/book/_updates-status).

How it works
------------
A daemon thread ticks every UPDATES_TICK_SECONDS (default 15 min). On each tick
it tries to *claim* the sweep via an atomic conditional UPDATE on a single
`scheduler_claims` row -- so even with multiple uvicorn workers or Railway
replicas sharing one Postgres, exactly ONE of them runs each interval
(UPDATES_SWEEP_GAP_SECONDS, default hourly). The claimer calls run_sweep, which
itself only triggers contacts that are actually DUE (vip = daily, others =
weekly). So a frequent tick never scrapes anyone more than their tier allows --
it just lowers the latency between "became due" and "checked". Cost tracks the
per-contact cadence, not the tick rate.

Safe by construction:
  * env-gated (UPDATES_SCHEDULER_ENABLED, default "1") -- flip off without a deploy
  * fail-soft -- a bad tick logs and the loop continues
  * restart-resilient -- due-ness is computed from watched_at vs wall-clock, so a
    process restart just re-evaluates on the next tick; nothing is lost.
"""
from __future__ import annotations

import os
import threading
import time

_THREAD: threading.Thread | None = None
_STARTED = False
_LAST_TICK: dict = {}


def _enabled() -> bool:
    return (os.environ.get("UPDATES_SCHEDULER_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off", ""))


def _tick_seconds() -> int:
    return max(60, int(os.environ.get("UPDATES_TICK_SECONDS", "900")))


def _gap_seconds() -> int:
    # Minimum wall-clock between two actual sweeps (the claim interval).
    return max(60, int(os.environ.get("UPDATES_SWEEP_GAP_SECONDS", "3600")))


def _limit() -> int:
    return max(1, min(int(os.environ.get("UPDATES_SWEEP_LIMIT", "200")), 1000))


def _demo_purge_gap_seconds() -> int:
    # How often to purge stale demo users (default hourly).
    return max(300, int(os.environ.get("DEMO_PURGE_GAP_SECONDS", "3600")))


def last_tick() -> dict:
    return _LAST_TICK


def _ensure_claim_table(conn) -> None:
    from sqlalchemy import text
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS scheduler_claims "
        "(name VARCHAR(64) PRIMARY KEY, last_run_at DOUBLE PRECISION NOT NULL)"))


def _claim(name: str, gap: float) -> bool:
    """Atomically claim a run: succeed iff no run happened within `gap` seconds.

    A single conditional UPDATE is the atomic primitive -- on Postgres and SQLite
    alike, exactly one concurrent caller's UPDATE matches the stale-row predicate,
    so only one worker/replica wins each interval. Returns True if WE won."""
    from sqlalchemy import text
    from ...db import ENGINE
    now = time.time()
    with ENGINE.begin() as conn:
        _ensure_claim_table(conn)
        # Seed the row once (ignore the race -- the UPDATE below decides the winner).
        conn.execute(text(
            "INSERT INTO scheduler_claims (name, last_run_at) VALUES (:n, 0) "
            "ON CONFLICT (name) DO NOTHING"), {"n": name})
        res = conn.execute(text(
            "UPDATE scheduler_claims SET last_run_at = :now "
            "WHERE name = :n AND last_run_at <= :cutoff"),
            {"now": now, "n": name, "cutoff": now - gap})
        return (res.rowcount or 0) >= 1


def run_claimed_sweep() -> dict:
    """Public entry: claim + run one sweep. Called by BOTH the in-process loop
    and the Modal scheduled function (modal_jobs.updates_sweep). They share the
    one `scheduler_claims` row, so whichever fires first within the gap wins and
    the other no-ops -- Modal is primary, the in-process thread is the fallback
    if Modal isn't deployed/reachable. Never double-fires."""
    return _run_once()


def _run_once() -> dict:
    """Claim + run one sweep. Returns a small status dict for diagnostics."""
    global _LAST_TICK
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    # Purge stale demo users on their own claim, independent of the sweep claim
    # (so it still runs when the sweep is claimed elsewhere or finds nothing due).
    if _claim("demo_purge", _demo_purge_gap_seconds()):
        try:
            from ...db import SessionLocal
            from ...routes.demo import _cleanup_stale_demo_users
            pdb = SessionLocal()
            try:
                n = _cleanup_stale_demo_users(pdb, limit=200)
                if n:
                    print(f"[updates.scheduler] purged {n} stale demo users", flush=True)
            finally:
                pdb.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[updates.scheduler] demo purge failed: {type(exc).__name__}: {exc}",
                  flush=True)
    # Proactive sweep (cadence + dated triggers) on its OWN claim, so it runs even
    # when the updates sweep is claimed elsewhere or finds nothing due.
    try:
        from .pipeline.proactive import run_claimed_proactive_sweep
        run_claimed_proactive_sweep()
    except Exception as exc:  # noqa: BLE001 : never let it sink the updates tick
        print(f"[updates.scheduler] proactive sweep error: "
              f"{type(exc).__name__}: {exc}", flush=True)
    # Message -> fact ingestion on its OWN claim. OFF by default (MESSAGE_INGEST_ENABLED);
    # no-ops cheaply when disabled, so this line is safe even before it's turned on.
    try:
        from .pipeline.context.extract import run_claimed_ingest_sweep
        run_claimed_ingest_sweep()
    except Exception as exc:  # noqa: BLE001 : never let it sink the updates tick
        print(f"[updates.scheduler] ingest sweep error: "
              f"{type(exc).__name__}: {exc}", flush=True)
    # LinkedIn Catch Up -> ContactFact (birthdays, job changes, …). ON by default;
    # own claim row, daily by default (CATCH_UP_INGEST_GAP_SECONDS=86400).
    try:
        from .pipeline.context.ingest.catch_up import run_claimed_catch_up_sweep
        run_claimed_catch_up_sweep()
    except Exception as exc:  # noqa: BLE001
        print(f"[updates.scheduler] catch_up ingest error: "
              f"{type(exc).__name__}: {exc}", flush=True)
    if not _claim("updates_sweep", _gap_seconds()):
        _LAST_TICK = {"at": stamp, "ran": False, "reason": "not due / claimed elsewhere"}
        return _LAST_TICK
    from ...db import SessionLocal
    from .updates_engine import run_sweep
    db = SessionLocal()
    try:
        res = run_sweep(db, user_id=None, limit=_limit())
        _LAST_TICK = {"at": stamp, "ran": True, "result": res}
        print(f"[updates.scheduler] swept: {res}", flush=True)
    except Exception as exc:  # noqa: BLE001 : a bad tick must never kill the loop
        _LAST_TICK = {"at": stamp, "ran": True, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[updates.scheduler] sweep failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        db.close()
    return _LAST_TICK


def _loop() -> None:
    # Small initial delay so startup/migrations settle before the first claim.
    time.sleep(min(60, _tick_seconds()))
    while True:
        try:
            run_claimed_sweep()
        except Exception as exc:  # noqa: BLE001
            print(f"[updates.scheduler] tick error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(_tick_seconds())


def start() -> None:
    """Launch the daemon thread once. No-op if disabled or already running."""
    global _THREAD, _STARTED
    if _STARTED:
        return
    if not _enabled():
        print("[updates.scheduler] disabled (UPDATES_SCHEDULER_ENABLED=0)", flush=True)
        return
    _STARTED = True
    _THREAD = threading.Thread(target=_loop, name="updates-scheduler", daemon=True)
    _THREAD.start()
    print(f"[updates.scheduler] started: tick={_tick_seconds()}s "
          f"gap={_gap_seconds()}s limit={_limit()}", flush=True)
