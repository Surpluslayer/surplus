"""
scripts/investor_outreach.py : drive the batched investor connection-request
campaign from the command line (against whatever DATABASE_URL points at).

The campaign roster + send logic live in
backend/agents/relationship/investor_campaign.py; this is a thin CLI over it.

    # Seed the campaign event + prospects (idempotent), print the roster:
    python -m scripts.investor_outreach seed

    # Dry-run preview: what WOULD be sent in the next batch (never sends):
    python -m scripts.investor_outreach preview --limit 12

    # Send for real — requires BOTH on the environment:
    #   UNIPILE_DRY_RUN=false   (else it's a dry-run)
    #   a connected LinkedIn account for the sender
    # Pick the sender with INVESTOR_OUTREACH_USER_EMAIL, or rely on the single
    # connected user. Include medium/low-confidence rows with --all.
    UNIPILE_DRY_RUN=false python -m scripts.investor_outreach send --limit 12

Safety: `send` still routes through the guarded path (double-send hold,
idempotency, 300-char check). It sends at most --limit invites per run; run it
once a day (or let the Modal `investor_outreach_sweep` schedule do it) so invite
volume stays under LinkedIn's limits.
"""
from __future__ import annotations

import argparse
import sys

from backend.db import SessionLocal, init_db
from backend.agents.relationship import investor_campaign as ic


def _sender(db):
    try:
        return ic.resolve_sender_user(db)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: set INVESTOR_OUTREACH_USER_EMAIL to the connected account.",
              file=sys.stderr)
        raise SystemExit(2)


def cmd_seed(db, args) -> None:
    user = _sender(db)
    ev, created = ic.seed_roster_event(db, user)
    print(f"event #{ev.id} '{ic.CAMPAIGN_EVENT_NAME}' — {len(ev.prospects)} "
          f"prospects ({created} new), sender={user.email}")
    for row in ic.load_roster():
        print(f"  [{row['confidence']:>6}] {row['name']:<22} {row['linkedin_url']}")


def cmd_preview(db, args) -> None:
    user = _sender(db)
    ev, _ = ic.seed_roster_event(db, user)
    high = ic.pending_count(db, ev, high_only=True)
    allc = ic.pending_count(db, ev, high_only=False)
    print(f"pending: {high} high-confidence, {allc} total (sender={user.email})")
    # A dry-run batch shows exactly which rows would go, without sending.
    import os
    forced = os.environ.get("UNIPILE_DRY_RUN")
    os.environ["UNIPILE_DRY_RUN"] = "true"
    try:
        summary = ic.run_batch(db, user=user, limit=args.limit,
                               high_only=not args.all, seed=False)
    finally:
        if forced is None:
            os.environ.pop("UNIPILE_DRY_RUN", None)
        else:
            os.environ["UNIPILE_DRY_RUN"] = forced
    print(f"next batch would attempt {summary['attempted']} (dry-run):")
    for r in summary["results"]:
        print(f"  -> {r['name']:<22} {r['state']}")


def cmd_send(db, args) -> None:
    user = _sender(db)
    summary = ic.run_batch(db, user=user, limit=args.limit, high_only=not args.all)
    tag = "DRY-RUN (nothing sent)" if summary["dry_run"] else "LIVE"
    print(f"[{tag}] sender={summary['sender']} "
          f"sent={summary['sent']}/{summary['attempted']} "
          f"remaining={summary['remaining']}")
    for r in summary["results"]:
        line = f"  -> {r['name']:<22} {r['state']}"
        if r["error"]:
            line += f"  ERR: {r['error']}"
        print(line)
    if summary["dry_run"]:
        print("note: set UNIPILE_DRY_RUN=false to send for real.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Investor outreach campaign CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed", help="create/refresh the campaign, list roster")
    p_seed.set_defaults(func=cmd_seed)

    for name, fn, helptext in (
        ("preview", cmd_preview, "show what the next batch would send (dry-run)"),
        ("send", cmd_send, "send the next batch (real only if UNIPILE_DRY_RUN=false)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--limit", type=int, default=None,
                       help="max invites this run (default INVESTOR_OUTREACH_DAILY_CAP=12)")
        p.add_argument("--all", action="store_true",
                       help="include medium/low-confidence rows (default: high only)")
        p.set_defaults(func=fn)

    args = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    try:
        args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
