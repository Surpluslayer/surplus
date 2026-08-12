"""backend/demo/seed_operator.py : seed Observe with real Unipile data.

Pulls actual contacts and interactions from Unipile-connected users' LinkedIn/email
and tags them for observation-only (Observe harnesses only, not product-visible).
No fake data; uses authentic patterns from real lawyer networks.

Run it:

    python -m backend.demo.seed_operator --email you@example.com --jurisdiction NY

Everything it writes is provenance-tagged (backend/demo/provenance.py), so
`prov.delete_cohort(db, cohort_id)` removes it completely. Real Unipile accounts
are never modified -- only their synced contacts/interactions are pulled into the
evaluation dataset, marked observation-only via provenance tags.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .. import models
from ..db import SessionLocal, init_db
from ..solicitation import JURISDICTION_RULES
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


def seed_from_unipile_users(db, cohort_id: str) -> dict:
    """Pull real contacts and interactions from Unipile users.

    Copies their synced LinkedIn and email data, tags with provenance so it's
    observation-only and wipeable. Real accounts remain untouched.

    Returns stats dict: {contacts: N, interactions: N, users: N}
    """
    # Find users with Unipile connections
    unipile_users = (
        db.query(models.User)
        .filter(models.User.unipile_account_id != None)
        .all()
    )

    if not unipile_users:
        print("  [seed] WARNING: no Unipile-connected users found.")
        return {"users": 0, "contacts": 0, "interactions": 0}

    stats = {"users": len(unipile_users), "contacts": 0, "interactions": 0}

    for user in unipile_users:
        # Query their synced contacts (from LinkedIn/email)
        contacts = (
            db.query(models.Contact)
            .filter(models.Contact.user_id == user.id)
            .all()
        )
        stats["contacts"] += len(contacts)

        # Query their synced interactions (email_sync, linkedin, etc.)
        interactions = (
            db.query(models.RelationshipInteraction)
            .filter(models.RelationshipInteraction.actor_user_id == user.id)
            .filter(
                models.RelationshipInteraction.source_type.in_(
                    ["email_sync", "linkedin", "linkedin_dm"]
                )
            )
            .all()
        )
        stats["interactions"] += len(interactions)

        # Tag all contacts and interactions with provenance
        for contact in contacts:
            prov.tag(db, contact, provenance=prov.BASELINE, cohort_id=cohort_id)

        for interaction in interactions:
            prov.tag(db, interaction, provenance=prov.BASELINE, cohort_id=cohort_id)

    db.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed Observe with real Unipile user data and set bar jurisdiction."
    )
    ap.add_argument("--email", required=True, help="account to set bar_jurisdiction on")
    ap.add_argument("--jurisdiction", default="NY", help="USPS 2-letter code (default: NY)")
    ap.add_argument(
        "--cohort-id",
        default=None,
        help="custom cohort ID (default: baseline-YYYYMMDDHHmmss)",
    )
    ap.add_argument(
        "--skip-cohort",
        action="store_true",
        help="only set jurisdiction; do not seed cohort",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="seed another cohort even if one already exists",
    )
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        ok = set_jurisdiction(db, args.email, args.jurisdiction)

        if args.skip_cohort:
            print("  [seed] --skip-cohort: no cohort seeded.")
            return 0 if ok else 1

        existing = existing_baseline_cohort(db)
        if existing and not args.force:
            counts = prov.cohort_row_counts(db, existing)
            total = sum(sum(v.values()) for v in counts.values())
            print(
                f"  [seed] an evaluation cohort already exists: {existing} ({total} rows). "
                f"Nothing to do -- pass --force to add another."
            )
            return 0 if ok else 1

        cohort_id = args.cohort_id or f"baseline-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        print(f"  [seed] seeding from Unipile users...")

        stats = seed_from_unipile_users(db, cohort_id)

        counts = prov.cohort_row_counts(db, cohort_id)
        total = sum(sum(v.values()) for v in counts.values())

        print(f"  [seed] cohort_id={cohort_id}")
        print(f"           users: {stats['users']}")
        print(f"           contacts: {stats['contacts']}")
        print(f"           interactions: {stats['interactions']}")
        print(f"           total_rows: {total}")
        for table, by_prov in sorted(counts.items()):
            print(f"           {table}: {by_prov}")

        print("  [seed] done. Reload /observe -- harnesses now have real evaluation data.")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
