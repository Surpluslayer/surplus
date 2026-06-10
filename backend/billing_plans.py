"""backend/billing_plans.py : subscription tiers + metered-usage logic.

Single source of truth for what each plan allows and whether a given user may
take one more metered action in the relationship layer (drafting a follow-up,
scanning a contact). Pure functions over the User row — no DB writes here; the
caller owns the transaction and commits after mutating the counters.

Two metered surfaces, both per billing period:
  - drafts_used_this_period          : +1 per staged follow-up DRAFT card
  - contacts_scanned_this_period     : +1 per contact the agent triages

Demo accounts (auth.is_demo_user) and any account in SURPLUS_UNLIMITED_ACCOUNTS
bypass the limits entirely, so live demos never hit a paywall mid-run.

This is deliberately independent of the legacy `paid_at` one-time unlock, which
gates real LinkedIn SENDS — a different surface with a different lifecycle.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

# Per-plan limits. `None` means unlimited. Kept as plain ints so the values are
# obvious at a glance and easy to tune for a demo.
PLAN_LIMITS: dict[str, dict[str, Optional[int]]] = {
    "free":      {"drafts": 5,    "contacts": 25},
    "starter":   {"drafts": 30,   "contacts": 250},
    "pro":       {"drafts": 200,  "contacts": 1000},
    # DB-controlled comp tier: `None` == no cap. Lets us grant a specific
    # account true unlimited from SQL alone (UPDATE users SET plan='unlimited')
    # without touching the SURPLUS_UNLIMITED_ACCOUNTS env allowlist.
    "unlimited": {"drafts": None, "contacts": None},
}

# Map a Stripe price id -> plan, via env so the same code works in test/live
# mode without a redeploy. Unknown / unmapped prices fall back to "free".
def price_to_plan(price_id: Optional[str]) -> str:
    if not price_id:
        return "free"
    starter = (os.environ.get("STRIPE_STARTER_PRICE_ID") or "").strip()
    pro = (os.environ.get("STRIPE_PRO_PRICE_ID") or "").strip()
    if price_id == pro:
        return "pro"
    if price_id == starter:
        return "starter"
    return "free"


def plan_of(user) -> str:
    """The user's plan, defaulting to 'free' for any unknown/missing value."""
    p = (getattr(user, "plan", None) or "free").strip().lower()
    return p if p in PLAN_LIMITS else "free"


def limits_for(user) -> dict[str, Optional[int]]:
    """Effective limits for this user. Unlimited accounts report None/None."""
    if is_unlimited(user):
        return {"drafts": None, "contacts": None}
    return dict(PLAN_LIMITS[plan_of(user)])


def _unlimited_allowlist() -> set[str]:
    raw = (os.environ.get("SURPLUS_UNLIMITED_ACCOUNTS") or "").strip()
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def is_unlimited(user) -> bool:
    """True for accounts that bypass metering: demo-link users and anything in
    SURPLUS_UNLIMITED_ACCOUNTS (match by email OR numeric id).

    SURPLUS_BILLING_DISABLED=1 makes EVERY account unlimited — the kill
    switch for environments where billing must not exist at all (the demo /
    staging deployment, so a live demo never sees a paywall)."""
    if (os.environ.get("SURPLUS_BILLING_DISABLED") or "").strip().lower()             in ("1", "true", "yes"):
        return True
    # Lazy import avoids a module-load cycle (auth imports models, not us).
    try:
        from .auth import is_demo_user
        if is_demo_user(user):
            return True
    except Exception:  # noqa: BLE001 : never let a flag check break a paid run
        pass
    allow = _unlimited_allowlist()
    if not allow:
        return False
    email = (getattr(user, "email", "") or "").strip().lower()
    uid = str(getattr(user, "id", "") or "")
    return email in allow or uid in allow


# ─── Billing period ──────────────────────────────────────────────────────────
# Free tier rolls a 30-day in-app window; paid tiers get their window stamped
# from Stripe by the webhook. Either way, when `now` passes the period end we
# reset the counters and (for the free roll) open a fresh window.
_FREE_PERIOD = timedelta(days=30)


def ensure_current_period(user, now: Optional[datetime] = None) -> bool:
    """Roll the user into the current billing period if the old one elapsed
    (or was never seeded). Mutates the user in place and returns True iff
    anything changed, so the caller knows whether to commit. No DB here."""
    now = now or datetime.now(timezone.utc)
    end = getattr(user, "billing_period_end", None)
    end = _aware(end)
    if end is not None and now < end:
        return False  # still inside the current window
    user.drafts_used_this_period = 0
    user.contacts_scanned_this_period = 0
    user.billing_period_start = now
    user.billing_period_end = now + _FREE_PERIOD
    return True


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat naive datetimes (SQLite round-trips drop tzinfo) as UTC so the
    `now < end` comparison never raises."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ─── Gate checks (pure reads) ────────────────────────────────────────────────

def can_generate_draft(user) -> bool:
    limit = limits_for(user)["drafts"]
    if limit is None:
        return True
    return int(getattr(user, "drafts_used_this_period", 0) or 0) < limit


def can_scan_contacts(user, requested: int = 1) -> bool:
    limit = limits_for(user)["contacts"]
    if limit is None:
        return True
    used = int(getattr(user, "contacts_scanned_this_period", 0) or 0)
    return used + max(0, int(requested)) <= limit


def remaining_drafts(user) -> Optional[int]:
    limit = limits_for(user)["drafts"]
    if limit is None:
        return None
    return max(0, limit - int(getattr(user, "drafts_used_this_period", 0) or 0))


def remaining_contacts(user) -> Optional[int]:
    limit = limits_for(user)["contacts"]
    if limit is None:
        return None
    return max(0, limit - int(getattr(user, "contacts_scanned_this_period", 0) or 0))


def record_usage(user, *, drafts: int = 0, contacts: int = 0) -> None:
    """Increment the period counters in place. Caller commits. Unlimited
    accounts still accumulate (harmless) so usage analytics stay truthful."""
    if drafts:
        user.drafts_used_this_period = (
            int(getattr(user, "drafts_used_this_period", 0) or 0) + int(drafts))
    if contacts:
        user.contacts_scanned_this_period = (
            int(getattr(user, "contacts_scanned_this_period", 0) or 0) + int(contacts))


def usage_snapshot(user) -> dict:
    """Compact billing view for /me and the paywall UI."""
    lim = limits_for(user)
    return {
        "plan": plan_of(user),
        "subscription_status": getattr(user, "subscription_status", None) or "free",
        "unlimited": is_unlimited(user),
        "limits": lim,
        "usage": {
            "drafts": int(getattr(user, "drafts_used_this_period", 0) or 0),
            "contacts": int(getattr(user, "contacts_scanned_this_period", 0) or 0),
        },
        "remaining": {
            "drafts": remaining_drafts(user),
            "contacts": remaining_contacts(user),
        },
        "billing_period_end": (
            user.billing_period_end.isoformat()
            if getattr(user, "billing_period_end", None) else None),
    }
