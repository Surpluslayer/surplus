"""agents/relationship/account_signals.py : the account-level proactive pass.

Phase 2 of the account layer (docs/accounts-architecture.md §5): signals that
only exist in AGGREGATE — no single contact's row can tell you that a whole
account went quiet. Runs inside the hourly sweep, after the per-contact pass.

v1 ships the highest-value play with zero new provider spend:

  ACCOUNT COOLING — the account had a real, recent relationship (a touch
  inside `memory_days`) but nothing across ANY of its contacts for
  `cooling_after_days`+. Emitted as an activity_update on the account's
  warmest contact so it surfaces in the existing Today/Updates feed. NOT
  auto-drafted (autodraft stays gated to job_change/new_post — a reconnect
  message is a judgment call the user should initiate, v1).

Also refreshes account rollups (strength/last-touch/count/warmest) so the
Accounts tab stays fresh from the sweep, not just from page views. Cross-
region discipline (§9.6 finding): aggregate reads are single IN-queries and
only CHANGED account rows are written — never a query or UPDATE per row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ... import models
from .relationship_watch import _emit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _refresh_rollups_for_user(db, user_id: int) -> int:
    """Recompute every account rollup for one user from TWO reads (edges with
    last-touch, then the account rows), writing only rows that changed.
    Mirrors accounts_read.recompute_rollups' formula:
    strength = mean over current members of (100 - min(days_since_touch, 100)).
    """
    from sqlalchemy import func

    edges = (db.query(models.AccountMembership.company_id,
                      models.AccountMembership.contact_id,
                      func.max(models.RelationshipInteraction.occurred_at))
               .outerjoin(models.RelationshipInteraction,
                          models.RelationshipInteraction.contact_id ==
                          models.AccountMembership.contact_id)
               .filter(models.AccountMembership.user_id == user_id,
                       models.AccountMembership.is_current.is_(True),
                       models.AccountMembership.status == "linked")
               .group_by(models.AccountMembership.company_id,
                         models.AccountMembership.contact_id)
               .all())

    per_company: dict[int, list] = {}
    for company_id, contact_id, last_touch in edges:
        per_company.setdefault(company_id, []).append(
            (contact_id, _as_utc(last_touch)))

    accounts = (db.query(models.Account)
                  .filter(models.Account.owner_type == "user",
                          models.Account.owner_id == user_id)
                  .all())
    now = _now()
    changed = 0
    for a in accounts:
        members = per_company.get(a.company_id, [])
        count = len(members)
        if count == 0:
            new = (0, None, None, None)
        else:
            touches = [t for _, t in members if t is not None]
            last_any = max(touches) if touches else None
            scores = []
            warmest, warmest_t = members[0][0], None
            for cid, t in members:
                days = 100.0 if t is None else min(
                    (now - t).total_seconds() / 86400.0, 100.0)
                scores.append(100.0 - days)
                if t is not None and (warmest_t is None or t > warmest_t):
                    warmest, warmest_t = cid, t
            new = (count, last_any,
                   round(sum(scores) / len(scores), 2), warmest)
        old = (a.contact_count, _as_utc(a.last_touch_at),
               a.strength_score, a.warmest_contact_id)
        if old != new:
            (a.contact_count, a.last_touch_at,
             a.strength_score, a.warmest_contact_id) = new
            changed += 1
    # autoflush=False session: push the pending rollup updates to the DB so
    # the cooling-candidate query that follows sees fresh values, not NULLs.
    db.flush()
    return changed


def account_pass(db, user_id: int | None = None, *,
                 cooling_after_days: int = 21, memory_days: int = 120,
                 max_emits: int = 10) -> dict:
    """Refresh rollups + emit account-cooling signals. Bounded and fail-soft:
    the sweep must never die on the account pass."""
    if user_id is not None:
        user_ids = [user_id]
    else:
        user_ids = [row[0] for row in
                    (db.query(models.Account.owner_id)
                       .filter(models.Account.owner_type == "user")
                       .distinct().all())]

    now = _now()
    refreshed = emitted = 0
    for uid in user_ids:
        try:
            refreshed += _refresh_rollups_for_user(db, uid)

            # Cooling = the account-wide LAST TOUCH (max across members —
            # the rollup just refreshed it) fell into the quiet window. No
            # strength gate: strength is a recency average, so it would just
            # re-encode the same age with a lag.
            candidates = (db.query(models.Account)
                            .filter(models.Account.owner_type == "user",
                                    models.Account.owner_id == uid,
                                    models.Account.contact_count > 0,
                                    models.Account.warmest_contact_id
                                    .isnot(None))
                            .all())
            cooling = []
            for a in candidates:
                last = _as_utc(a.last_touch_at)
                if last is None:
                    continue  # never a real relationship -> dormant, not cooling
                age = (now - last).days
                if cooling_after_days <= age <= memory_days:
                    cooling.append(a)
            if not cooling:
                db.commit()
                continue

            # Dedup: one cooling nudge per account per cooling window — the
            # feed must never nag about the same silence twice in a row.
            warmest_ids = [a.warmest_contact_id for a in cooling]
            since = now - timedelta(days=cooling_after_days)
            already = {
                row[0] for row in
                (db.query(models.RelationshipInteraction.contact_id)
                   .filter(models.RelationshipInteraction.contact_id
                           .in_(warmest_ids),
                           models.RelationshipInteraction.interaction_type ==
                           "account_cooling",
                           models.RelationshipInteraction.occurred_at >=
                           since)
                   .all())}

            companies = {c.id: c for c in
                         (db.query(models.Company)
                            .filter(models.Company.id.in_(
                                [a.company_id for a in cooling]))
                            .all())}
            for a in cooling:
                if emitted >= max_emits:
                    break
                if a.warmest_contact_id in already:
                    continue
                contact = db.get(models.Contact, a.warmest_contact_id)
                company = companies.get(a.company_id)
                if contact is None or company is None:
                    continue
                days = (now - _as_utc(a.last_touch_at)).days
                n = a.contact_count
                summary = (f"{company.canonical_name} is cooling: no touch "
                           f"with your {n} contact{'s' if n != 1 else ''} "
                           f"there in {days} days")
                _emit(db, contact, "account_cooling", summary,
                      {"company_id": a.company_id,
                       "company": company.canonical_name,
                       "account_id": a.id, "days_quiet": days,
                       "source": "account_pass"})
                emitted += 1
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [account_pass] user={uid} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    return {"rollups_changed": refreshed, "cooling_emitted": emitted}
