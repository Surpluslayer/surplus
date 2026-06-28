"""agents/relationship/proactive.py : the proactive surface -- "what's due now".

Unifies the two deterministic triggers into one feed the rest of the system reads:
  * DATED triggers  -- contact_memory.due_facts (birthday, an upcoming flight)
  * CADENCE          -- cadence.due_contacts (relationship gone quiet past its tier)

Two entry points:
  collect_due(db, user_id)      -- READ-ONLY snapshot for one user (the feed the UI
                                   and the harness pull). Consumes nothing.
  run_proactive_sweep(db, ...)  -- the periodic job across all users. Collect-only by
                                   default; pass on_due to FIRE dated triggers (which
                                   then consume via scan_and_fire). Whether a nudge
                                   actually sends is gated by the automation flag --
                                   this layer decides WHO/WHAT is due, not whether to
                                   send. That seam (on_due) is where the harness plugs
                                   in.

Mirrors updates_scheduler: claim-guarded so exactly one worker/replica fires each
interval, fail-soft, env-gated. Modal-primary + in-process fallback via one
`scheduler_claims` row (claim name "proactive_sweep").
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Optional

from ..... import models
from . import cadence
from ... import triggers
from ...spine import memory as contact_memory


def _trigger_row(fact, contact) -> dict:
    return {
        "contact_id": fact.contact_id,
        "name": getattr(contact, "name", None),
        "key": fact.key,
        "value": fact.value,
        "due_date": fact.due_date,
        "recurring": bool(fact.recurring),
        "source": fact.source,
    }


def collect_due(db, user_id: int, *, now: Optional[datetime] = None,
                within_days: int = 0, cadence_limit: int = 50) -> dict:
    """READ-ONLY snapshot of everything due for one user, right now.

    `contacts_due` = relationship maintenance (cadence), most-overdue first.
    `triggers_due` = dated facts firing within the lookahead (birthday, flight).
    Consumes nothing -- the dated triggers are NOT marked fired here (that happens
    only when something acts on them, via run_proactive_sweep's on_due)."""
    now = now or datetime.now(timezone.utc)
    contacts_due = cadence.due_contacts(
        db, user_id, now=now, within_days=within_days, limit=cadence_limit)
    facts = contact_memory.due_facts(
        db, now=now, user_id=user_id, within_days=within_days)
    triggers_due = []
    for f in facts:
        c = db.get(models.Contact, f.contact_id) if f.contact_id else None
        triggers_due.append(_trigger_row(f, c))
    return {
        "contacts_due": contacts_due,
        "triggers_due": triggers_due,
        "counts": {"contacts": len(contacts_due), "triggers": len(triggers_due)},
    }


# How a dated trigger reads as a one-line "why reach out". Unknown keys fall back
# to a generic phrasing so a new fact kind never breaks the plan.
_TRIGGER_PHRASES = {
    "birthday": "It's {name}'s birthday",
    "work_anniversary": "{name}'s work anniversary",
    "upcoming_travel": "{name} has upcoming travel: {value}",
    "flight": "{name} has an upcoming flight: {value}",
}


def _trigger_reason(t: dict) -> str:
    name = t.get("name") or "They"
    phrase = _TRIGGER_PHRASES.get(t.get("key"), "{name}: {key} ({value})")
    return phrase.format(name=name, key=t.get("key"),
                         value=(t.get("value") or "")).strip()


def daily_plan(db, user_id: int, *, now: Optional[datetime] = None,
               within_days: int = 1, limit: int = 25) -> dict:
    """The unified 'who to reach out to' list, DEDUPED + prioritized.

    Merges the two proactive sources into one action list: a DATED trigger (a
    birthday today) outranks CADENCE staleness, and a contact due for BOTH appears
    ONCE with the trigger as the reason. Each item: contact_id, name, kind
    (trigger|cadence), reason, priority (0 = trigger, 1 = cadence). Read-only;
    `within_days` lets 'today's plan' include tomorrow's dated triggers."""
    from .. import proactive as _proactive
    snap = _proactive.collect_due(db, user_id, now=now, within_days=within_days,
                                  cadence_limit=max(limit, 50))
    plan: dict = {}
    for t in snap["triggers_due"]:
        cid = t.get("contact_id")
        if cid is None or cid in plan:
            continue
        plan[cid] = {"contact_id": cid, "name": t.get("name"), "kind": "trigger",
                     "trigger": t.get("key"), "reason": _trigger_reason(t),
                     "priority": 0, "overdue_ratio": None}
    for c in snap["contacts_due"]:
        cid = c.get("contact_id")
        if cid is None or cid in plan:       # a trigger already claimed this contact
            continue
        plan[cid] = {"contact_id": cid, "name": c.get("name"), "kind": "cadence",
                     "trigger": None, "reason": c.get("reason"),
                     "priority": 1, "overdue_ratio": c.get("overdue_ratio")}
    items = sorted(plan.values(),
                   key=lambda x: (x["priority"], -(x["overdue_ratio"] or 0.0)))
    return {"count": len(items), "plan": items[:limit]}


def _user_ids_with_contacts(db) -> list[int]:
    rows = db.query(models.Contact.user_id).distinct().all()
    return [r[0] for r in rows if r[0] is not None]


def run_proactive_sweep(db=None, *, now: Optional[datetime] = None,
                        within_days: int = 0,
                        on_due: Optional[Callable] = None) -> dict:
    """Sweep every user: collect cadence (read-only) + handle dated triggers.

    on_due=None  -> collect-only (counts dated triggers via due_facts, consumes
                    nothing). The safe default for the background heartbeat.
    on_due set   -> fire each due dated trigger through triggers.scan_and_fire,
                    which calls on_due(fact, contact) then consumes it. on_due is
                    the harness/automation seam (compose + gated send live there).

    Returns a per-user + total summary. Fail-soft per user."""
    now = now or datetime.now(timezone.utc)
    own_db = db is None
    if own_db:
        from .....db import SessionLocal
        db = SessionLocal()
    try:
        per_user: list[dict] = []
        tot_contacts = tot_triggers = tot_expired = 0
        for uid in _user_ids_with_contacts(db):
            try:
                # GC ancient one-off triggers nobody consumed (collect-only never
                # fires them), so they don't pile up as zombies in the feed.
                tot_expired += contact_memory.expire_stale(db, user_id=uid, now=now)
                contacts_due = cadence.due_contacts(
                    db, uid, now=now, within_days=within_days)
                if on_due is None:
                    fired = contact_memory.due_facts(
                        db, now=now, user_id=uid, within_days=within_days)
                    n_trig = len(fired)
                else:
                    n_trig = len(triggers.scan_and_fire(
                        db, user_id=uid, now=now, within_days=within_days,
                        on_due=on_due))
            except Exception as exc:  # noqa: BLE001 : one bad user can't sink the sweep
                per_user.append({"user_id": uid, "error":
                                 f"{type(exc).__name__}: {exc}"})
                continue
            tot_contacts += len(contacts_due)
            tot_triggers += n_trig
            if contacts_due or n_trig:
                per_user.append({"user_id": uid, "contacts_due": len(contacts_due),
                                 "triggers_due": n_trig})
        return {"users": len(per_user), "contacts_due": tot_contacts,
                "triggers_due": tot_triggers, "expired": tot_expired,
                "fired": on_due is not None, "detail": per_user}
    finally:
        if own_db:
            db.close()


# ── scheduling (mirrors updates_scheduler's claim guard) ──────────────────────

def _enabled() -> bool:
    return (os.environ.get("PROACTIVE_SCHEDULER_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off", ""))


def _gap_seconds() -> int:
    # Minimum wall-clock between two actual proactive sweeps.
    return max(60, int(os.environ.get("PROACTIVE_SWEEP_GAP_SECONDS", "3600")))


_LAST_TICK: dict = {}


def last_tick() -> dict:
    return _LAST_TICK


def run_claimed_proactive_sweep() -> dict:
    """Claim + run one proactive sweep. Shared by the in-process loop and (future)
    Modal function via the "proactive_sweep" claim row, so it never double-fires.
    Collect-only (on_due=None): the heartbeat that keeps the feed warm and is the
    seam where gated auto-fire will hook in. No-op when disabled / claimed elsewhere."""
    global _LAST_TICK
    stamp = datetime.now(timezone.utc).isoformat()
    if not _enabled():
        _LAST_TICK = {"at": stamp, "ran": False, "reason": "disabled"}
        return _LAST_TICK
    from ...updates_scheduler import _claim  # reuse the one atomic claim primitive
    if not _claim("proactive_sweep", _gap_seconds()):
        _LAST_TICK = {"at": stamp, "ran": False, "reason": "not due / claimed elsewhere"}
        return _LAST_TICK
    try:
        res = run_proactive_sweep()
        _LAST_TICK = {"at": stamp, "ran": True, "result": res}
        print(f"[proactive.scheduler] swept: {res.get('users')} users, "
              f"{res.get('contacts_due')} cadence + {res.get('triggers_due')} triggers due",
              flush=True)
    except Exception as exc:  # noqa: BLE001 : a bad tick must never kill the loop
        _LAST_TICK = {"at": stamp, "ran": True, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[proactive.scheduler] sweep failed: {type(exc).__name__}: {exc}",
              flush=True)
    return _LAST_TICK
