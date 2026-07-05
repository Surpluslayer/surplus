"""
scripts/backfill_accounts.py : sweep existing Contacts through the company
resolver (agents/relationship/company_resolve.py) so the account layer
materializes from relationships that already exist.

    python -m backend.scripts.backfill_accounts                    # dry run, all users
    python -m backend.scripts.backfill_accounts --user-id 171      # dry run, one user
    python -m backend.scripts.backfill_accounts --execute          # actually write
    python -m backend.scripts.backfill_accounts --database-url postgresql://...

WHY THIS EXISTS
---------------
Company / CompanyIdentity / AccountMembership / Account rows are created
lazily by resolve_contact at the ingest hooks (capture, enrichment, job-change
detection) -- contacts that predate the account layer never pass through those
hooks, so their accounts do not exist. This one-shot runs the SAME resolver
over the existing spine.

SAFE BY CONSTRUCTION
--------------------
- DRY RUN BY DEFAULT: without --execute the sweep computes the full report and
  rolls back, so you always see the plan (counts + a 15-row sample) before any
  row lands. Print, inspect, then re-run with --execute.
- Idempotent: resolve_contact upserts memberships, so re-running creates
  nothing new the second time.
- Everything lands with source="backfill" and its resolver confidence, so a
  bad sweep is auditable and reversible.
- With no ANTHROPIC_API_KEY the deterministic paths still run end to end;
  ambiguous names simply land as pending_review instead of LLM-arbitrated.
"""
from __future__ import annotations

import argparse
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill the account layer from existing contacts.")
    parser.add_argument("--user-id", type=int, default=None,
                        help="Limit the sweep to one user's contacts.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually write. Default is a dry run that "
                             "rolls back and only prints the report.")
    parser.add_argument("--database-url",
                        default=os.environ.get("DATABASE_URL") or "",
                        help="Database URL (default: env DATABASE_URL; falls "
                             "back to the local SQLite file when unset).")
    args = parser.parse_args()

    # db.py builds its engine from DATABASE_URL at import time, so the
    # override must be in the environment BEFORE backend.db is imported.
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    from ..db import SessionLocal, init_db
    from ..agents.relationship.company_resolve import backfill

    init_db()
    db = SessionLocal()
    try:
        report = backfill(db, user_id=args.user_id, dry_run=not args.execute)
        report["dry_run"] = not args.execute
        print(json.dumps(report, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
