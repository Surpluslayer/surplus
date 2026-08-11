"""backend/migrate.py : apply DB migrations as a standalone one-off step.

Railway runs this as the deploy.preDeployCommand, BEFORE the new app version
starts serving. That moves the schema work off the app's boot path, so the
first boot after a schema change is a fast no-op (init_db's schema-rev fast
path skips the whole migration loop) and /api/health passes the deploy
healthcheck immediately.

Why this exists: on 2026-08-11 a deploy failed its healthcheck because the
first container after a schema change ran the full migration loop (create_all +
every _migrate_* + the inspector round-trips) synchronously in the lifespan on
the single uvicorn worker, starving /api/health past the 15-minute window. The
migration itself was fine; the boot was just too busy to answer health. Running
migrations here, once, before the app serves, fixes that class of failure for
every future schema change.

Properties:
  - Idempotent + cheap when the schema is already current (one SELECT via the
    schema-rev fast path), so it is safe to run on EVERY deploy.
  - Single process (not per-replica), so no replica races on the DDL.
  - Exits non-zero on a real migration failure, so the deploy fails LOUDLY at
    the pre-deploy step and prod keeps serving the old version, instead of
    shipping a half-applied schema.

init_db still runs in the app lifespan as the fallback (local dev, tests, or if
the pre-deploy step is ever not configured); once the schema is current it is
the same fast no-op there.
"""
from __future__ import annotations

import sys


def main() -> int:
    from .db import init_db
    print("[migrate] pre-deploy: running init_db ...", flush=True)
    init_db()
    print("[migrate] pre-deploy: init_db complete, schema is current.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[migrate] pre-deploy FAILED: {type(exc).__name__}: {exc}",
              flush=True)
        sys.exit(1)
