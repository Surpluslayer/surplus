"""backend/demo/seed_operator.py : point Observe's harnesses at a REAL account.

Two separable jobs, and they fail independently:

  1. set the account's bar_jurisdiction (drives the solicitation gate), and
  2. reference that account's real, already-synced contacts and interactions
     into an evaluation cohort so the data-driven harnesses evaluate actual
     data instead of a generated one.

Run it:

    python -m backend.demo.seed_operator --email you@example.com --jurisdiction NY

NOTHING HERE GENERATES DATA. It tags rows that already exist, produced by the
normal sync path. That is why the tags say OBSERVED ("observed_real_account_
data") rather than BASELINE ("generated_from_assumed_distribution") -- the
latter would be a false statement about where the rows came from, in the one
table whose whole job is recording where rows came from.

It also means cleanup is untagging, not deletion: prov.delete_cohort() skips
OBSERVED rows by construction and removes only their tags. An earlier version
of this module tagged real rows as BASELINE and told operators to clean up
with delete_cohort; doing so deleted a real account's entire book.
"""
from __future__ import annotations

import argparse
import json

from datetime import datetime, timezone

from sqlalchemy import select

from .. import models
from ..db import SessionLocal, init_db
from ..solicitation import JURISDICTION_RULES
from . import provenance as prov


def set_practice_area(db, email: str, practice_area: str | None) -> bool:
    """practice_area drives practice_fit. Unset, affinity() returns a neutral
    0.5 for every contact -- so a fifth of the opportunity score carries no
    information and the pipeline logs a fallback on every single trace. It is
    one column, and nothing else sets it."""
    if not practice_area:
        return True
    from . import signal_taxonomy as tax
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        return False
    area = practice_area.strip().lower()
    if area not in tax.PRACTICE_AREAS:
        print(f"  [seed] WARNING: {area!r} is not one of "
              f"{', '.join(tax.PRACTICE_AREAS)} -- practice_fit will fall back to "
              f"a neutral 0.5 for every signal category.")
    before = user.practice_area
    user.practice_area = area
    db.commit()
    print(f"  [seed] {email}: practice_area {before!r} -> {area!r}")
    return True


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


def _evaluable_contacts(db, contacts: list) -> int:
    """How many of these contacts the data-driven harnesses can actually score.

    ablation / relationship_evaluation / historical_replay grade against a
    contact's most recent drafted follow-up (see observe/cohort_query.py). A
    freshly synced contact has none until the updates engine has drafted for
    it, so a cohort can be large and still evaluate nothing -- worth
    reporting up front rather than letting the boot log show a mysterious 0/0.

    Matches BOTH draft shapes (production stores the draft on the
    detected-signal row's meta_json; the demo cohort writes a separate titled
    row), or this undercounts a real account to zero.
    """
    if not contacts:
        return 0
    from ..agents.relationship import outcomes as _outcomes
    ids = [c.id for c in contacts]
    found: set = set()
    for i in range(0, len(ids), 400):
        rows = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id.in_(ids[i:i + 400]),
                   _outcomes.drafted_filter())
        ).scalars().all()
        found.update(r.contact_id for r in rows if _outcomes.is_drafted(r))
    return len(found)


def seed_from_real_account(db, email: str, cohort_id: str) -> dict:
    """Reference ONE account's real contacts and interactions into `cohort_id`.

    Scoped to the named account on purpose. Tagging every Unipile-connected
    user (as an earlier version did) mixes several people's books into one
    evaluation set, which is both a privacy surprise and meaningless to
    evaluate: Observe is account-scoped everywhere else.

    The USER row is tagged too. Without that tag
    observe/cohort_query.users_and_contacts() finds no lawyers for the cohort
    and every data-driven harness silently reports 0/0 -- which is exactly
    what the previous version did while printing that it had succeeded.
    """
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        return {"users": 0, "contacts": 0, "interactions": 0, "evaluable_contacts": 0}

    if not user.unipile_account_id:
        print(f"  [seed] NOTE: {email} has no Unipile connection. Referencing whatever "
              f"contacts the account already has; if that is zero, connect an account "
              f"and let it sync first.")

    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()

    interactions: list = []
    ids = [c.id for c in contacts]
    for i in range(0, len(ids), 400):
        interactions.extend(db.execute(
            select(models.RelationshipInteraction).where(
                models.RelationshipInteraction.contact_id.in_(ids[i:i + 400]))
        ).scalars().all())

    prov.tag(db, user, provenance=prov.OBSERVED, cohort_id=cohort_id)
    for c in contacts:
        prov.tag(db, c, provenance=prov.OBSERVED, cohort_id=cohort_id)
    for r in interactions:
        prov.tag(db, r, provenance=prov.OBSERVED, cohort_id=cohort_id)
    db.commit()

    return {
        "users": 1,
        "contacts": len(contacts),
        "interactions": len(interactions),
        "evaluable_contacts": _evaluable_contacts(db, contacts),
    }


def seed_from_unipile_users(db, cohort_id: str) -> dict:
    """Reference EVERY Unipile-connected account's data into `cohort_id`.

    The broad form, kept because an operator staging a multi-account
    evaluation set genuinely wants it. Not the default: pooling several
    people's books into one cohort is a privacy decision, and Observe is
    account-scoped everywhere else, so it lives behind --all-unipile-users
    rather than happening implicitly.

    Tags the lawyer THEMSELVES, not just their rows: cohort_query.
    users_and_contacts() resolves a cohort's lawyers strictly from a
    table_name=="users" tag, so without it every harness silently found zero
    lawyers no matter how much contact data was tagged underneath.
    """
    unipile_users = db.execute(select(models.User).where(
        models.User.unipile_account_id.isnot(None))).scalars().all()
    if not unipile_users:
        print("  [seed] WARNING: no Unipile-connected users found.")
        return {"users": 0, "contacts": 0, "interactions": 0, "evaluable_contacts": 0}

    stats = {"users": len(unipile_users), "contacts": 0,
             "interactions": 0, "evaluable_contacts": 0}
    for user in unipile_users:
        contacts = db.execute(select(models.Contact).where(
            models.Contact.user_id == user.id)).scalars().all()
        interactions = db.execute(select(models.RelationshipInteraction).where(
            models.RelationshipInteraction.actor_user_id == user.id,
            models.RelationshipInteraction.source_type.in_(
                ("email_sync", "linkedin", "linkedin_dm")))).scalars().all()

        prov.tag(db, user, provenance=prov.OBSERVED, cohort_id=cohort_id)
        for c in contacts:
            prov.tag(db, c, provenance=prov.OBSERVED, cohort_id=cohort_id)
        for r in interactions:
            prov.tag(db, r, provenance=prov.OBSERVED, cohort_id=cohort_id)

        stats["contacts"] += len(contacts)
        stats["interactions"] += len(interactions)
        stats["evaluable_contacts"] += _evaluable_contacts(db, contacts)
    db.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Set an account's bar jurisdiction and reference its real data "
                    "into an Observe evaluation cohort.")
    ap.add_argument("--email", required=True,
                    help="the account to set bar_jurisdiction on AND seed from")
    ap.add_argument("--jurisdiction", default="NY", help="USPS 2-letter code (default: NY)")
    ap.add_argument("--practice-area", default=None,
                    help="drives practice_fit; unset means a neutral 0.5 on every "
                         "trace (corporate_ma, litigation, real_estate, "
                         "employment_labor, ip, regulatory)")
    ap.add_argument("--cohort-id", default=None,
                    help="custom cohort ID (default: observed-YYYYMMDDHHmmss)")
    ap.add_argument("--skip-cohort", action="store_true",
                    help="only set jurisdiction; do not touch the evaluation cohort")
    ap.add_argument("--force", action="store_true",
                    help="seed another cohort even if one already exists")
    ap.add_argument("--all-unipile-users", action="store_true",
                    help="reference EVERY Unipile-connected account, not just --email "
                         "(pools several people's books into one evaluation cohort)")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        ok = set_jurisdiction(db, args.email, args.jurisdiction)
        set_practice_area(db, args.email, args.practice_area)

        if args.skip_cohort:
            print("  [seed] --skip-cohort: no cohort seeded.")
            return 0 if ok else 1
        if not ok:
            print("  [seed] account not found -- nothing to seed from.")
            return 1

        existing = existing_baseline_cohort(db)
        if existing and not args.force:
            counts = prov.cohort_row_counts(db, existing)
            total = sum(sum(v.values()) for v in counts.values())
            print(f"  [seed] an evaluation cohort already exists: {existing} ({total} rows). "
                  f"Nothing to do -- pass --force to add another.")
            return 0

        cohort_id = args.cohort_id or f"observed-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        if args.all_unipile_users:
            print("  [seed] referencing EVERY Unipile-connected account ...")
            stats = seed_from_unipile_users(db, cohort_id)
        else:
            print(f"  [seed] referencing real data from {args.email} ...")
            stats = seed_from_real_account(db, args.email, cohort_id)

        counts = prov.cohort_row_counts(db, cohort_id)
        total = sum(sum(v.values()) for v in counts.values())
        print(f"  [seed] cohort_id={cohort_id}")
        print(f"           users:        {stats['users']}")
        print(f"           contacts:     {stats['contacts']}")
        print(f"           interactions: {stats['interactions']}")
        print(f"           total_rows:   {total}")
        for table, by_prov in sorted(counts.items()):
            print(f"           {table}: {by_prov}")

        # Report what the harnesses will actually be able to do, rather than
        # asserting success. A cohort with no drafted follow-ups produces a
        # boot log full of 0/0 and no explanation for it.
        evaluable = stats["evaluable_contacts"]
        if evaluable:
            print(f"  [seed] {evaluable} of {stats['contacts']} contacts have a drafted "
                  f"follow-up to grade against -- the data-driven harnesses can evaluate "
                  f"those. Reload /observe.")
        else:
            print(f"  [seed] WARNING: none of the {stats['contacts']} contacts has a "
                  f"'Drafted follow-up' interaction yet, so ablation / "
                  f"relationship_evaluation / historical_replay will report 0/0 -- they "
                  f"grade against drafted outcomes (see observe/cohort_query.py). "
                  f"jurisdiction_regression and signal_library_evaluation do not need "
                  f"this data and will still run. Let the updates engine draft for this "
                  f"account first, or use a generated cohort "
                  f"(python -m backend.demo.cohort) to exercise the harnesses.")

        print("  [seed] cleanup, if ever needed: prov.delete_cohort(db, "
              f"{cohort_id!r}) -- removes the TAGS only; OBSERVED rows are real "
              "account data and are never deleted.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
