"""
investor_campaign.py : a batched, throttled LinkedIn connection-request campaign
against a fixed, hand-curated investor roster (backend/data/investor_outreach.json).

Why this is its own module and not the event pipeline: the target list is a
static set of investors with pre-written, per-person connection notes, not an
ICP-scored prospect pool. There's nothing to prospect or score. But the *send*
still routes through the ONE guarded path (`send.flow.route_and_send`), so every
invite inherits the double-send hold, the 300-char note check, dry-run gating,
and an OutreachLog row — exactly like any other send in the product.

Safety rails (deliberately conservative — this fires real, irreversible LinkedIn
invites to real people):

  * DRY-RUN by default. The provider is dry-run unless UNIPILE_DRY_RUN=false.
    Nothing leaves the box until that is explicitly flipped.
  * DISABLED by default. The daily Modal job no-ops unless
    INVESTOR_OUTREACH_ENABLED=true.
  * Daily cap. At most INVESTOR_OUTREACH_DAILY_CAP invites per run (default 12),
    well under LinkedIn's ~weekly invite ceiling, spread out by the daily
    schedule so a burst never looks robotic.
  * Idempotent. A roster entry that already has an invite_sent / unconfirmed /
    message_sent OutreachLog is never re-sent.
  * Confidence gate. Only entries marked confidence=="high" auto-send. The
    handful of medium/low rows (ambiguous profile matches like a company page or
    a common name) are seeded but held out of the automated batch for a human to
    eyeball first; send them explicitly with high_only=False once confirmed.
  * Jitter between sends.

Entry points:
  seed_roster_event(db, user)      -> (Event, created_count)  : idempotent seed
  run_batch(db, *, limit, ...)     -> dict summary            : send the next N
  pending_count(db, event, ...)    -> int
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Optional

from ... import models
from ..outreach import Message
from ...providers import get_provider_for_user

_ROSTER_PATH = Path(__file__).parents[2] / "data" / "investor_outreach.json"

# One dedicated event holds the whole campaign so the roster is isolated from a
# user's real events. Matched by (user_id, event_name) so re-seeding is stable.
CAMPAIGN_EVENT_NAME = "Investor outreach · belated July 4"
CAMPAIGN_CITY = "New York"

# Sent-state markers: any of these on a linkedin OutreachLog means "already
# reached out, do not re-send".
_ALREADY = ("invite_sent", "message_sent", "follow_up_sent", "unconfirmed")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def load_roster() -> list[dict]:
    """The curated investor list. Raises if the data file is missing/corrupt."""
    return json.loads(_ROSTER_PATH.read_text())


# ── sender resolution ────────────────────────────────────────────────────────

def resolve_sender_user(db) -> models.User:
    """The user whose connected LinkedIn account sends the invites.

    Prefers INVESTOR_OUTREACH_USER_EMAIL (explicit, unambiguous). Otherwise
    falls back to the single user with an active LinkedIn connection — and
    refuses if there's more than one, rather than guessing whose account
    blasts 59 investors.
    """
    email = (os.environ.get("INVESTOR_OUTREACH_USER_EMAIL") or "").strip().lower()
    if email:
        user = (db.query(models.User)
                .filter(models.User.email.ilike(email))
                .one_or_none())
        if user is None:
            raise LookupError(f"no user with email {email!r}")
        if not getattr(user, "unipile_account_id", None):
            raise LookupError(f"user {email!r} has no connected LinkedIn account")
        return user

    connected = (db.query(models.User)
                 .filter(models.User.unipile_account_id.isnot(None),
                         models.User.unipile_account_id != "")
                 .all())
    if not connected:
        raise LookupError("no user has a connected LinkedIn account")
    if len(connected) > 1:
        raise LookupError(
            f"{len(connected)} users have LinkedIn connected — set "
            "INVESTOR_OUTREACH_USER_EMAIL to pick the sender explicitly")
    return connected[0]


# ── seeding ──────────────────────────────────────────────────────────────────

def _get_or_create_event(db, user) -> models.Event:
    ev = (db.query(models.Event)
          .filter(models.Event.user_id == user.id,
                  models.Event.event_name == CAMPAIGN_EVENT_NAME)
          .one_or_none())
    if ev is not None:
        return ev
    ev = models.Event(
        user_id=user.id,
        kind="planned",
        event_name=CAMPAIGN_EVENT_NAME,
        label=CAMPAIGN_EVENT_NAME,
        city=CAMPAIGN_CITY,
        format="Mixer",
        goal="Fundraising",
        role="Investor",
        seniority="Leadership",
        co_stage="pre-seed",
        headcount=len(load_roster()),
        sources="linkedin",
    )
    db.add(ev)
    db.flush()  # need ev.id for the prospects
    return ev


def seed_roster_event(db, user) -> tuple[models.Event, int]:
    """Create the campaign event + one Prospect per roster entry, idempotently.

    Upserts on (event_id, identity): re-running refreshes the note/url/role in
    place (so edits to the roster propagate) without duplicating rows or
    resetting send state. Returns (event, number_of_rows_created).
    """
    roster = load_roster()
    ev = _get_or_create_event(db, user)

    existing = {p.identity: p for p in ev.prospects}
    created = 0
    for row in roster:
        ident = row["identity"]
        note = (row.get("note") or "").strip()
        p = existing.get(ident)
        if p is None:
            p = models.Prospect(
                event_id=ev.id,
                identity=ident,
                name=row["name"],
                role=row.get("role") or "Investor",
                company=row.get("company") or "",
                seniority="Leadership",
                side="Invests",
                works_on="venture",
                linkedin_url=row.get("linkedin_url"),
                li_resolved=bool(row.get("linkedin_url")),
                sources="linkedin",
                note=note,
                private_note=f"confidence={row.get('confidence', 'unknown')}",
                status="approved",
                connection_status="unknown",
            )
            db.add(p)
            created += 1
        else:
            # Refresh mutable fields in place; leave send state untouched.
            p.name = row["name"]
            p.role = row.get("role") or p.role
            p.company = row.get("company") or p.company
            p.linkedin_url = row.get("linkedin_url") or p.linkedin_url
            p.note = note or p.note
            p.private_note = f"confidence={row.get('confidence', 'unknown')}"
    db.commit()
    return ev, created


# ── selection + send ─────────────────────────────────────────────────────────

def _roster_confidence() -> dict[str, str]:
    return {r["identity"]: r.get("confidence", "unknown") for r in load_roster()}


def _already_reached(db, prospect_id: int) -> bool:
    row = (db.query(models.OutreachLog.id)
           .filter(models.OutreachLog.prospect_id == prospect_id,
                   models.OutreachLog.channel == "linkedin",
                   models.OutreachLog.state.in_(_ALREADY))
           .first())
    return row is not None


def _pending(db, event, *, high_only: bool) -> list[models.Prospect]:
    conf = _roster_confidence()
    out: list[models.Prospect] = []
    for p in sorted(event.prospects, key=lambda x: x.id):
        if p.status == "contacted":
            continue
        if high_only and conf.get(p.identity) != "high":
            continue
        if _already_reached(db, p.id):
            continue
        out.append(p)
    return out


def pending_count(db, event, *, high_only: bool = True) -> int:
    return len(_pending(db, event, high_only=high_only))


def run_batch(
    db,
    *,
    user=None,
    limit: Optional[int] = None,
    high_only: bool = True,
    seed: bool = True,
    min_gap_s: float = 8.0,
    max_gap_s: float = 20.0,
) -> dict:
    """Send the next `limit` pending invites through the guarded send path.

    Honors the provider's dry-run flag end to end: under UNIPILE_DRY_RUN
    (the default) nothing is sent, every row logs as dry_run_queued, and no
    status flips — so this is safe to run for a preview. Flip UNIPILE_DRY_RUN
    to false (on the deployed backend, where Unipile egress is allowed) to
    send for real.

    Returns a summary dict; each result carries the prospect, the resulting
    OutreachLog state, and any error.
    """
    from .pipeline.send.flow import route_and_send

    if limit is None:
        limit = _env_int("INVESTOR_OUTREACH_DAILY_CAP", 12)

    user = user or resolve_sender_user(db)
    provider = get_provider_for_user(user)
    event, created = seed_roster_event(db, user) if seed else (
        _get_or_create_event(db, user), 0)

    queue = _pending(db, event, high_only=high_only)[:limit]

    results: list[dict] = []
    for i, p in enumerate(queue):
        draft = Message(note=p.note or "", message=p.note or "")
        try:
            outcome = route_and_send(
                db, p, provider, event=event,
                draft=draft, refresh_connection=True, commit=True)
            results.append({
                "identity": p.identity, "name": p.name,
                "state": outcome.res.state, "path": outcome.path_taken,
                "error": outcome.res.error,
            })
        except Exception as exc:  # noqa: BLE001 — one bad row must not halt the batch
            db.rollback()
            results.append({
                "identity": p.identity, "name": p.name,
                "state": "error", "path": None, "error": str(exc),
            })
        # Jitter between sends so the batch isn't a robotic burst. Skip the
        # wait under dry-run (no network, no rate limit to respect) and after
        # the last item.
        if not getattr(provider, "dry_run", True) and i < len(queue) - 1:
            time.sleep(random.uniform(min_gap_s, max_gap_s))

    sent = sum(1 for r in results if r["state"] in ("invite_sent", "message_sent"))
    return {
        "event_id": event.id,
        "sender": getattr(user, "email", None),
        "dry_run": bool(getattr(provider, "dry_run", True)),
        "seeded": created,
        "attempted": len(results),
        "sent": sent,
        "remaining": pending_count(db, event, high_only=high_only),
        "results": results,
    }
