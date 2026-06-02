"""
scripts/backfill_contacts.py

One-time backfill: link every already-*touched* Prospect to a durable Contact
on the canonical relationship spine, so cross-event recall works for people we
met before the auto-link wiring existed.

A prospect is "touched" (belongs in the relationship graph) when we've actually
engaged them — captured in person, sent/received any LinkedIn outreach, become a
connection, or moved past discovery. Pure never-contacted discovery prospects are
left alone so the graph stays "who we've met", not "who we surfaced".

Idempotent + fail-soft: link_contact() de-dupes on the strong identity key and
no-ops for prospects with no LinkedIn/email identity, so re-running is safe.

    python3 -m scripts.backfill_contacts            # apply
    python3 -m scripts.backfill_contacts --dry-run  # report only, no writes
"""
from __future__ import annotations
import sys

# Statuses that, on their own, imply we've engaged the person.
_TOUCHED_STATUSES = {"pending", "contacted", "rsvp", "converted", "replied"}


def _is_touched(p) -> bool:
    if getattr(p, "captured_at", None) is not None:
        return True
    if getattr(p, "connection_status", None) == "connected":
        return True
    if (getattr(p, "status", None) or "") in _TOUCHED_STATUSES:
        return True
    if list(getattr(p, "outreach", None) or []):
        return True
    return False


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    from backend.db import SessionLocal, init_db
    from backend import models
    from backend.agents.relationships import link_contact

    init_db()
    db = SessionLocal()
    seen = linked = skipped_untouched = skipped_no_identity = no_owner = 0
    try:
        for p in db.query(models.Prospect).all():
            seen += 1
            if not _is_touched(p):
                skipped_untouched += 1
                continue
            owner_id = getattr(getattr(p, "event", None), "user_id", None)
            if owner_id is None:
                no_owner += 1
                continue
            if getattr(p, "contact_id", None) is not None:
                linked += 1  # already on the spine
                continue
            if dry_run:
                # We can't know without identity_keys whether it would link, so
                # just count it as a candidate; the real run reports precisely.
                linked += 1
                continue
            contact = link_contact(db, p, owner_id)
            if contact is None:
                skipped_no_identity += 1
            else:
                linked += 1

        print(
            f"prospects={seen} linked/on-spine={linked} "
            f"untouched-skipped={skipped_untouched} "
            f"no-strong-identity={skipped_no_identity} no-owner={no_owner}"
            + ("  (dry-run, no writes)" if dry_run else "")
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
