#!/usr/bin/env python3
"""Read-only inventory of a Surplus database.

Counts every table and reports the "connection health" signals that matter for
deciding whether two databases can be unified (LinkedIn / email / Unipile
connections, users, events, captures). It runs ONLY SELECT COUNT queries: it
never writes, alters, or drops anything, so it is safe to point at production.

Usage
-----
  # against whatever DATABASE_URL is in the environment (e.g. on Railway):
  railway run python backend/scripts/db_inventory.py

  # or pass a URL explicitly (local):
  python backend/scripts/db_inventory.py "postgresql://user:pass@host/dbname"

Run it once for the event.surpluslayer.com database and once for the
surpluslayer.com database, then compare the two reports. The DB whose numbers
are non-trivial (real users + active connections) is the canonical one to keep.
"""
import os
import sys

from sqlalchemy import create_engine, MetaData, select, func, text, inspect


def _normalize(url: str) -> str:
    # Railway/Heroku hand out postgres://… ; SQLAlchemy 2.x wants postgresql://
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("DATABASE_URL") or "")
    url = _normalize(url.strip())
    if not url:
        print("ERROR: no database URL. Pass one as an argument or set DATABASE_URL.",
              file=sys.stderr)
        return 2

    # Mask credentials in the banner so this is safe to paste into a chat.
    safe = url
    if "@" in safe and "//" in safe:
        head, tail = safe.split("//", 1)
        creds_host = tail.split("@", 1)
        if len(creds_host) == 2:
            safe = f"{head}//***@{creds_host[1]}"
    print(f"\n=== Surplus DB inventory : {safe} ===\n")

    engine = create_engine(url)
    md = MetaData()
    # Reflect the ACTUAL schema in the database (not the app's models), so this
    # works even if the two DBs are at slightly different migration states.
    md.reflect(bind=engine)

    with engine.connect() as conn:
        tables = sorted(md.tables.keys())
        if not tables:
            print("(no tables found — this database is empty)\n")
            return 0

        print(f"{'table':32} {'rows':>10}")
        print("-" * 44)
        total = 0
        counts = {}
        for name in tables:
            tbl = md.tables[name]
            n = conn.execute(select(func.count()).select_from(tbl)).scalar() or 0
            counts[name] = n
            total += n
            print(f"{name:32} {n:>10,}")
        print("-" * 44)
        print(f"{'TOTAL':32} {total:>10,}\n")

        # Connection-health signals : only if a recognizable users table exists.
        if "users" in md.tables:
            users = md.tables["users"]
            cols = {c.name for c in users.columns}

            def count_where(clause_sql):
                try:
                    q = select(func.count()).select_from(users).where(text(clause_sql))
                    return conn.execute(q).scalar() or 0
                except Exception:
                    return "n/a"

            print("Connection signals (users):")
            print(f"  total users ............. {counts.get('users', 0):,}")
            if "linkedin_status" in cols:
                v = count_where("linkedin_status = 'active'")
                print(f"  LinkedIn active ......... {v}")
            if "unipile_account_id" in cols:
                v = count_where("unipile_account_id IS NOT NULL")
                print(f"  Unipile (LinkedIn) set .. {v}")
            if "email_status" in cols:
                v = count_where("email_status = 'active'")
                print(f"  Email/Gmail active ...... {v}")
            if "unipile_email_account_id" in cols:
                v = count_where("unipile_email_account_id IS NOT NULL")
                print(f"  Unipile (email) set ..... {v}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
