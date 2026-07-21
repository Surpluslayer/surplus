"""agents/updates_scheduler.py : in-process scheduler for the updates sweep.

Replaces the flaky external GitHub-Actions cron with a background thread that
lives INSIDE the product. No external secrets, no missed/delayed scheduled runs,
and it's visible to the app (status via /api/book/_diagnostics).

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


def _gathering_gap_seconds() -> int:
    # How often to re-gather conversation context (LinkedIn DMs + email
    # correspondents) per user. Default every 6h; claim-guarded like the
    # updates sweep so replicas never double-run it.
    return max(600, int(os.environ.get("GATHERING_SWEEP_GAP_SECONDS", "21600")))


def _gathering_user_limit() -> int:
    # Cap the users touched per sweep so one tick can't queue unbounded
    # Unipile I/O. The next sweep picks up where cadence leaves off (the
    # LinkedIn watermark makes repeat visits cheap).
    return max(1, min(int(os.environ.get("GATHERING_SWEEP_USER_LIMIT", "25")), 200))


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


def run_gathering_sweep(limit: int = 25) -> dict:
    """One gathering pass: for each user with an active LinkedIn or email
    seat, run the INCREMENTAL LinkedIn DM sync (watermarked, idempotent by
    message id) and the email correspondents re-sync (rollup rows updated in
    place, so re-runs are idempotent too). This is what keeps the per-contact
    context the drafter reads fresh between connect-time first syncs.

    Conservative by construction: each user is wrapped in try/except (one
    flaky account never kills the sweep), the user count is capped per pass,
    and both syncs carry their own per-account page/chat caps so a pass can't
    hammer Unipile. Returns a small stats dict for the tick log."""
    from sqlalchemy import or_

    from ... import models
    from ...db import SessionLocal
    from ...jobs import _unipile_env
    from .email_sync import sync_email_contacts
    from .linkedin_chat_sync import sync_linkedin_chats

    out = {"users": 0, "linkedin": 0, "email": 0, "errors": 0}
    dsn, api_key = _unipile_env()
    if not (dsn and api_key):
        out["reason"] = "unipile not configured"
        return out

    db = SessionLocal()
    try:
        users = (db.query(models.User)
                 .filter(models.User.is_demo.is_(False))
                 .filter(or_(
                     (models.User.unipile_account_id.isnot(None))
                     & (models.User.linkedin_status == "active"),
                     models.User.email_status == "active",
                     models.User.unipile_email_account_id.isnot(None),
                 ))
                 .order_by(models.User.id.asc())
                 .limit(limit)
                 .all())
        for user in users:
            out["users"] += 1
            # LinkedIn DMs -> per-contact message timeline (incremental).
            try:
                if user.unipile_account_id and user.linkedin_status == "active":
                    stats = sync_linkedin_chats(db, user, dsn=dsn,
                                                api_key=api_key,
                                                incremental=True)
                    if stats.get("error"):
                        out["errors"] += 1
                    else:
                        out["linkedin"] += 1
            except Exception as exc:  # noqa: BLE001 : one user never kills the sweep
                out["errors"] += 1
                print(f"[gathering] linkedin sync user={user.id} failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            # Email correspondents -> contact spine rollups (in-place update).
            try:
                has_email = (user.email_status == "active"
                             or user.unipile_email_account_id
                             or models.list_email_accounts(db, user))
                if has_email:
                    stats = sync_email_contacts(db, user, dsn=dsn,
                                                api_key=api_key)
                    if stats.get("error"):
                        out["errors"] += 1
                    else:
                        out["email"] += 1
            except Exception as exc:  # noqa: BLE001
                out["errors"] += 1
                print(f"[gathering] email sync user={user.id} failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
    finally:
        db.close()
    return out


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
    # Gathering sweep (LinkedIn DM sync + email correspondent re-sync) on its
    # OWN claim, every ~6h. Conservative: per-user try/except inside, so one
    # user's flaky account never kills the sweep or this tick.
    if _claim("gathering_sweep", _gathering_gap_seconds()):
        try:
            res = run_gathering_sweep(limit=_gathering_user_limit())
            print(f"[updates.scheduler] gathering sweep: {res}", flush=True)
        except Exception as exc:  # noqa: BLE001 : never let it sink the updates tick
            print(f"[updates.scheduler] gathering sweep error: "
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


def _initial_delay_seconds() -> int:
    """How long a FRESH container waits before its first tick, keeping the
    boot healthcheck probes clear (railway.json healthcheckPath=/api/health,
    healthcheckTimeout=600s; the probe passes on the first 200, normally well
    inside this delay): the sweeps run in this process, and a heavy pass
    (gathering: per-user LinkedIn + email sync)
    during boot starves /api/health on the single worker. That exact failure
    took down deploy 247f9eb2 on 2026-07-01 (4m51s of "service unavailable"
    while the first tick churned). Steady-state cadence is unaffected: claims
    are shared, so an already-running replica keeps ticking on schedule."""
    raw = (os.environ.get("UPDATES_SCHEDULER_INITIAL_DELAY_SECONDS") or "").strip()
    try:
        return max(0, int(raw)) if raw else 420
    except ValueError:
        return 420


def _loop() -> None:
    # Sit out the deploy healthcheck window before the first claim (see
    # _initial_delay_seconds); a new container must be HEALTHY before it is
    # allowed to spend CPU on sweeps.
    time.sleep(_initial_delay_seconds())
    while True:
        try:
            run_claimed_sweep()
        except Exception as exc:  # noqa: BLE001
            print(f"[updates.scheduler] tick error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(_tick_seconds())


def _followup_tick_seconds() -> int:
    """Cadence of the PUNCTUAL follow-up dispatcher (default 60s). The pass is
    one cheap indexed query when nothing is due, so a tight tick is fine; the
    payoff is that a staged follow-up fires within a minute of its send_at
    instead of whenever GitHub's hourly cron deigns to run."""
    raw = (os.environ.get("FOLLOWUP_DISPATCH_TICK_SECONDS") or "").strip()
    try:
        return max(15, int(raw)) if raw else 60
    except ValueError:
        return 60


def _followup_loop() -> None:
    # Same boot etiquette as the main loop: stay quiet through the deploy
    # healthcheck window before the first claim.
    time.sleep(_initial_delay_seconds())
    from ...db import SessionLocal
    while True:
        try:
            tick = _followup_tick_seconds()
            if _claim("followup_dispatch", max(15, tick - 5)):
                from ...routes.admin import dispatch_due_followups
                db = SessionLocal()
                try:
                    res = dispatch_due_followups(db)
                    # Held rows sit in the queue for days by design (waiting on
                    # the autonomy gate / manual send): only log a pass that
                    # actually CHANGED something, or every minute is noise.
                    if (res.get("sent") or res.get("failed")
                            or res.get("cancelled")):
                        print(f"[followup.dispatch] {res}", flush=True)
                finally:
                    db.close()
        except Exception as exc:  # noqa: BLE001 : a bad pass must never kill the loop
            print(f"[followup.dispatch] error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(_followup_tick_seconds())


_FOLLOWUP_THREAD: threading.Thread | None = None


def start() -> None:
    """Launch the daemon threads once. No-op if disabled or already running."""
    global _THREAD, _FOLLOWUP_THREAD, _STARTED
    if _STARTED:
        return
    if not _enabled():
        print("[updates.scheduler] disabled (UPDATES_SCHEDULER_ENABLED=0)", flush=True)
        return
    _STARTED = True
    _THREAD = threading.Thread(target=_loop, name="updates-scheduler", daemon=True)
    _THREAD.start()
    # Punctual follow-up dispatcher on its own thread + claim, so a due
    # follow-up sends within ~a minute of its send_at (the external GitHub
    # cron stays as redundancy; claims + per-row status flips dedupe).
    _FOLLOWUP_THREAD = threading.Thread(
        target=_followup_loop, name="followup-dispatch", daemon=True)
    _FOLLOWUP_THREAD.start()
    print(f"[updates.scheduler] started: tick={_tick_seconds()}s "
          f"gap={_gap_seconds()}s limit={_limit()} "
          f"followup_tick={_followup_tick_seconds()}s", flush=True)
