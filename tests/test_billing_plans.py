"""Tests for backend/billing_plans.py — the plan-limit + metered-usage logic.

Pins the things a paywall must get exactly right: limits per tier, the
demo/allowlist unlimited bypass, period rollover resetting counters, and the
gate checks firing at the boundary (not one off). All pure-function, no DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend import billing_plans as bp


def _user(**kw):
    base = dict(
        id=1, email="real@person.com", plan="free", subscription_status="free",
        drafts_used_this_period=0, contacts_scanned_this_period=0,
        billing_period_start=None, billing_period_end=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── limits + plan resolution ─────────────────────────────────────────────────

def test_limits_per_plan():
    assert bp.limits_for(_user(plan="free")) == {"drafts": 5, "contacts": 25}
    assert bp.limits_for(_user(plan="starter")) == {"drafts": 30, "contacts": 250}
    assert bp.limits_for(_user(plan="pro")) == {"drafts": 200, "contacts": 1000}


def test_unlimited_plan_tier_has_no_caps():
    # DB-controlled comp tier: plan='unlimited' bypasses caps with no env var.
    u = _user(plan="unlimited", drafts_used_this_period=9999,
              contacts_scanned_this_period=9999)
    assert bp.limits_for(u) == {"drafts": None, "contacts": None}
    assert bp.can_generate_draft(u) is True
    assert bp.can_scan_contacts(u, 100000) is True
    assert bp.remaining_drafts(u) is None


def test_unknown_plan_falls_back_to_free():
    assert bp.plan_of(_user(plan="enterprise_megacorp")) == "free"
    assert bp.plan_of(_user(plan=None)) == "free"


def test_price_to_plan_maps_via_env(monkeypatch):
    monkeypatch.setenv("STRIPE_STARTER_PRICE_ID", "price_starter")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro")
    assert bp.price_to_plan("price_starter") == "starter"
    assert bp.price_to_plan("price_pro") == "pro"
    assert bp.price_to_plan("price_unknown") == "free"
    assert bp.price_to_plan(None) == "free"


# ── unlimited bypass ─────────────────────────────────────────────────────────

def test_demo_user_is_unlimited(monkeypatch):
    # is_demo_user keys off the demo email domain; a demo user bypasses limits
    # even when way over the numeric cap.
    demo = _user(email="visitor@demo.surpluslayer.com",
                 drafts_used_this_period=9999)
    assert bp.is_unlimited(demo) is True
    assert bp.can_generate_draft(demo) is True
    assert bp.limits_for(demo) == {"drafts": None, "contacts": None}


def test_allowlist_unlimited_by_email_or_id(monkeypatch):
    monkeypatch.setenv("SURPLUS_UNLIMITED_ACCOUNTS", "vip@x.com, 42")
    assert bp.is_unlimited(_user(email="vip@x.com")) is True
    assert bp.is_unlimited(_user(id=42, email="other@x.com")) is True
    assert bp.is_unlimited(_user(id=7, email="nobody@x.com")) is False


def test_normal_user_is_not_unlimited(monkeypatch):
    monkeypatch.delenv("SURPLUS_UNLIMITED_ACCOUNTS", raising=False)
    assert bp.is_unlimited(_user()) is False


# ── gate checks at the boundary ──────────────────────────────────────────────

def test_can_generate_draft_blocks_exactly_at_limit():
    assert bp.can_generate_draft(_user(plan="free", drafts_used_this_period=4)) is True
    assert bp.can_generate_draft(_user(plan="free", drafts_used_this_period=5)) is False
    assert bp.can_generate_draft(_user(plan="free", drafts_used_this_period=6)) is False


def test_can_scan_contacts_accounts_for_requested_batch():
    u = _user(plan="free", contacts_scanned_this_period=20)  # limit 25
    assert bp.can_scan_contacts(u, 5) is True     # 20+5 == 25, fits
    assert bp.can_scan_contacts(u, 6) is False    # 20+6 > 25
    assert bp.can_scan_contacts(u, 1) is True


def test_remaining_helpers():
    u = _user(plan="starter", drafts_used_this_period=10,
              contacts_scanned_this_period=40)
    assert bp.remaining_drafts(u) == 20      # 30 - 10
    assert bp.remaining_contacts(u) == 210   # 250 - 40
    # unlimited -> None
    vip = _user(email="visitor@demo.surpluslayer.com")
    assert bp.remaining_drafts(vip) is None


# ── usage accounting ─────────────────────────────────────────────────────────

def test_record_usage_increments_in_place():
    u = _user(drafts_used_this_period=2, contacts_scanned_this_period=10)
    bp.record_usage(u, drafts=3, contacts=12)
    assert u.drafts_used_this_period == 5
    assert u.contacts_scanned_this_period == 22


# ── period rollover ──────────────────────────────────────────────────────────

def test_ensure_period_seeds_a_window_on_first_call():
    u = _user(billing_period_end=None)
    changed = bp.ensure_current_period(u)
    assert changed is True
    assert u.billing_period_start is not None
    assert u.billing_period_end > u.billing_period_start


def test_ensure_period_is_noop_inside_window():
    now = datetime.now(timezone.utc)
    u = _user(billing_period_start=now - timedelta(days=1),
              billing_period_end=now + timedelta(days=29),
              drafts_used_this_period=3)
    changed = bp.ensure_current_period(u, now=now)
    assert changed is False
    assert u.drafts_used_this_period == 3  # untouched


def test_ensure_period_resets_counters_when_window_elapsed():
    now = datetime.now(timezone.utc)
    u = _user(billing_period_start=now - timedelta(days=40),
              billing_period_end=now - timedelta(days=10),
              drafts_used_this_period=5, contacts_scanned_this_period=25)
    changed = bp.ensure_current_period(u, now=now)
    assert changed is True
    assert u.drafts_used_this_period == 0
    assert u.contacts_scanned_this_period == 0
    assert u.billing_period_end > now


def test_ensure_period_handles_naive_datetimes():
    """SQLite drops tzinfo; the comparison must not raise on naive values."""
    now = datetime.now(timezone.utc)
    u = _user(billing_period_end=(now + timedelta(days=5)).replace(tzinfo=None))
    # naive end in the future -> still inside window, no reset, no crash
    assert bp.ensure_current_period(u, now=now) is False


# ── snapshot shape ───────────────────────────────────────────────────────────

def test_usage_snapshot_shape():
    u = _user(plan="starter", drafts_used_this_period=4,
              contacts_scanned_this_period=10)
    snap = bp.usage_snapshot(u)
    assert snap["plan"] == "starter"
    assert snap["limits"] == {"drafts": 30, "contacts": 250}
    assert snap["usage"] == {"drafts": 4, "contacts": 10}
    assert snap["remaining"] == {"drafts": 26, "contacts": 240}
    assert snap["unlimited"] is False


def test_billing_disabled_env_makes_everyone_unlimited(monkeypatch):
    """SURPLUS_BILLING_DISABLED=1 : the demo/staging kill switch — no
    paywall, no caps, for ANY account."""
    monkeypatch.setenv("SURPLUS_BILLING_DISABLED", "1")
    u = _user(plan="free", drafts_used_this_period=9999)
    assert bp.is_unlimited(u) is True
    assert bp.can_generate_draft(u) is True
    assert bp.usage_snapshot(u)["unlimited"] is True
