"""backend/demo/seed_operator.py : one operator command to make Observe
show real numbers on a deployed environment.

Two things Observe needs that a fresh deploy does not have, and that CI
cannot do for you because both write to the live database:

  1. An evaluation dataset. The four data-driven harnesses (historical
     replay, ablation, relationship evaluation, signal library evaluation)
     score a generated cohort; with none present they SKIP and the page
     honestly reports that it has nothing to evaluate against.
  2. A bar jurisdiction on the account you sign in as. Without it the
     solicitation gate resolves to the fail-closed unknown-jurisdiction rule
     and every jurisdiction line reads as "NOT SET" rather than exercising a
     real state's rule set.

Run it once against the deployed database:

    python -m backend.demo.seed_operator --email you@example.com --jurisdiction NY

    # bigger cohort / longer window
    python -m backend.demo.seed_operator --email you@example.com \
        --jurisdiction NY --lawyers 80 --days 30

    # jurisdiction only, no cohort (real book already has data)
    python -m backend.demo.seed_operator --email you@example.com \
        --jurisdiction NY --skip-cohort

Everything it writes is provenance-tagged (backend/demo/provenance.py), so
`prov.delete_cohort(db, cohort_id)` removes it completely. It does NOT touch
any contact, interaction, or draft belonging to a real account -- the only
write against a real account is the single bar_jurisdiction column.
"""
from __future__ import annotations

import argparse
import sys

from .. import models
from ..db import SessionLocal, init_db
from ..solicitation import JURISDICTION_RULES
from . import cohort as cohort_mod
from . import provenance as prov


def set_jurisdiction(db, email: str, jurisdiction: str) -> bool:
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        print(f"  [seed] NO ACCOUNT with email {email!r} -- jurisdiction NOT set.")
        emails = [e for (e,) in db.query(models.User.email).limit(25).all()]
        if emails:
            print("  [seed] accounts present in this database:")
            for e in emails:
                print(f"           {e}")
        return False

    code = jurisdiction.strip().upper()
    if code not in JURISDICTION_RULES:
        print(f"  [seed] WARNING: {code!r} has no entry in solicitation.JURISDICTION_RULES "
              f"(known: {', '.join(sorted(JURISDICTION_RULES))}). Setting it anyway; the gate "
              f"will resolve it to the fail-closed unknown-jurisdiction rule.")

    before = user.bar_jurisdiction
    user.bar_jurisdiction = code
    db.commit()
    print(f"  [seed] {email}: bar_jurisdiction {before!r} -> {code!r}")
    return True


def existing_baseline_cohort(db):
    from ..observe.logstream import evaluation_cohort_id
    return evaluation_cohort_id(db)


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed an evaluation cohort and set an account's bar jurisdiction.")
    ap.add_argument("--email", required=True, help="account to set bar_jurisdiction on")
    ap.add_argument("--jurisdiction", default="NY", help="USPS 2-letter code (default: NY)")
    ap.add_argument("--lawyers", type=int, default=80)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cohort-id", default=None)
    ap.add_argument("--skip-cohort", action="store_true",
                    help="only set the jurisdiction; do not generate a cohort")
    ap.add_argument("--force", action="store_true",
                    help="generate another cohort even if one already exists")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        ok = set_jurisdiction(db, args.email, args.jurisdiction)

        if args.skip_cohort:
            print("  [seed] --skip-cohort: no cohort generated.")
            return 0 if ok else 1

        existing = existing_baseline_cohort(db)
        if existing and not args.force:
            counts = prov.cohort_row_counts(db, existing)
            total = sum(sum(v.values()) for v in counts.values())
            print(f"  [seed] an evaluation cohort already exists: {existing} ({total} rows). "
                  f"Nothing to do -- pass --force to add another.")
            return 0 if ok else 1

        print(f"  [seed] generating cohort: {args.lawyers} lawyers x {args.days} days ...")
        cohort_id = cohort_mod.generate(db, n_lawyers=args.lawyers, days=args.days,
                                        cohort_id=args.cohort_id)
        counts = prov.cohort_row_counts(db, cohort_id)
        total = sum(sum(v.values()) for v in counts.values())
        print(f"  [seed] cohort_id={cohort_id} ({total} rows)")
        for table, by_prov in sorted(counts.items()):
            print(f"           {table}: {by_prov}")
        print("  [seed] done. Reload /observe -- the four data-driven harnesses will "
              "now have an evaluation dataset.")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
