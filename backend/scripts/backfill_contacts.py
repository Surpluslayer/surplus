"""
scripts/backfill_contacts.py : build the durable Contact spine from existing
prospects.

    python -m backend.scripts.backfill_contacts --user 171   # one user
    python -m backend.scripts.backfill_contacts --all         # every user
    python -m backend.scripts.backfill_contacts --user 171 --dry-run

WHY THIS EXISTS
---------------
A `Contact` (the cross-event "person you've met") is created lazily by
agents.relationships.link_contact, which only fires at three points : in-person
capture (routes/inperson.py), a Unipile webhook (routes/webhooks.py), and an
actual message send (agents/sender.py). Prospects surfaced by prospecting/import
that never hit those paths therefore have NO Contact row, so the Relationships
page ("/api/relationships/contacts") shows nothing even though the prospects
exist. This one-shot walks every prospect and runs the SAME link_contact, so the
spine reflects history.

SAFE BY CONSTRUCTION
--------------------
- link_contact is idempotent (skips a prospect that's already linked) and only
  creates a Contact when a STRONG identity key is derivable. Prospect rows carry
  only `linkedin_url` (no email column), so in practice a Contact is created iff
  the prospect has a linkedin_url; otherwise it's skipped, by design.
- NO network / LinkedIn / Unipile calls. Pure local DB read+write.
- Re-runnable: running twice creates nothing new the second time.
"""
from __future__ import annotations

import argparse

from ..db import SessionLocal, init_db
from .. import models
from ..agents import relationships


def _prospects_for_user(db, user_id: int):
    return (db.query(models.Prospect)
              .join(models.Event, models.Prospect.event_id == models.Event.id)
              .filter(models.Event.user_id == user_id)
              .all())


def _user_ids_with_events(db) -> list[int]:
    rows = (db.query(models.Event.user_id)
              .filter(models.Event.user_id.isnot(None))
              .distinct()
              .all())
    return [r[0] for r in rows]


def backfill_user(db, user_id: int, *, dry_run: bool) -> dict:
    prospects = _prospects_for_user(db, user_id)
    stats = {"user_id": user_id, "prospects": len(prospects),
             "already_linked": 0, "eligible": 0, "linked": 0, "skipped_no_identity": 0}

    for p in prospects:
        if getattr(p, "contact_id", None) is not None:
            stats["already_linked"] += 1
            continue
        has_identity = bool((getattr(p, "linkedin_url", None) or "").strip())
        if not has_identity:
            stats["skipped_no_identity"] += 1
            continue
        stats["eligible"] += 1
        if dry_run:
            continue
        contact = relationships.link_contact(db, p, user_id)
        if contact is not None:
            stats["linked"] += 1
        else:
            # identity present but link_contact declined (rare) — count as skip
            stats["skipped_no_identity"] += 1

    return stats


def _print(stats: dict, dry_run: bool) -> None:
    verb = "WOULD link" if dry_run else "linked"
    print(f"  user {stats['user_id']:>4}: {stats['prospects']:>4} prospects | "
          f"{stats['already_linked']:>4} already linked | "
          f"{stats['eligible']:>4} eligible | {verb} {stats['linked']:>4} | "
          f"{stats['skipped_no_identity']:>4} skipped (no linkedin_url)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the Contact spine from prospects.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", type=int, help="backfill a single user_id")
    g.add_argument("--all", action="store_true", help="backfill every user with events")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be linked; write nothing")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        user_ids = [args.user] if args.user is not None else _user_ids_with_events(db)
        mode = "DRY-RUN (no writes)" if args.dry_run else "WRITING"
        print(f"[backfill_contacts] {mode} — {len(user_ids)} user(s)")
        totals = {"prospects": 0, "eligible": 0, "linked": 0,
                  "already_linked": 0, "skipped_no_identity": 0}
        for uid in user_ids:
            s = backfill_user(db, uid, dry_run=args.dry_run)
            _print(s, args.dry_run)
            for k in totals:
                totals[k] += s[k]
        print(f"[backfill_contacts] DONE — prospects={totals['prospects']} "
              f"eligible={totals['eligible']} linked={totals['linked']} "
              f"already_linked={totals['already_linked']} "
              f"skipped={totals['skipped_no_identity']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
