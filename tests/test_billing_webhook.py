"""Tests for the Stripe webhook's subscription handling (routes/billing.py).

Pins the recurring relationship-layer plan path that's independent of the
legacy one-time paid_at unlock:
  - price → plan mapping + billing-window stamping (_apply_subscription)
  - counters reset ONLY when the period actually rolls (so frequent
    subscription.updated events don't wipe mid-period usage)
  - checkout.session.completed branches subscription-mode vs one-time
  - customer.subscription.updated / .deleted update / downgrade the user
  - user resolution by stripe_subscription_id then stripe_customer_id

The Stripe SDK is stubbed — we test OUR webhook logic, not Stripe.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import billing as billing_route


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    defaults = dict(name="Op", email="op@real.com", unipile_account_id="acct1",
                    plan="free")
    defaults.update(kw)
    u = models.User(**defaults)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _sub(*, sub_id="sub_1", price="price_starter", status="active",
         customer="cus_1", start=None, end=None):
    now = datetime.now(timezone.utc)
    start = start if start is not None else now
    end = end if end is not None else now + timedelta(days=30)
    return {
        "id": sub_id,
        "status": status,
        "customer": customer,
        "current_period_start": int(start.timestamp()),
        "current_period_end": int(end.timestamp()),
        "items": {"data": [{"price": {"id": price}}]},
    }


@pytest.fixture(autouse=True)
def _price_env(monkeypatch):
    monkeypatch.setenv("STRIPE_STARTER_PRICE_ID", "price_starter")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro")


# ── _apply_subscription (pure stamping) ──────────────────────────────────────

def test_apply_subscription_maps_price_and_window(db):
    u = _user(db)
    billing_route._apply_subscription(u, _sub(price="price_pro"))
    assert u.plan == "pro"
    assert u.subscription_status == "active"
    assert u.stripe_subscription_id == "sub_1"
    assert u.stripe_price_id == "price_pro"
    assert u.stripe_customer_id == "cus_1"
    assert u.billing_period_end > u.billing_period_start


def test_apply_subscription_unknown_price_falls_back_free(db):
    u = _user(db)
    billing_route._apply_subscription(u, _sub(price="price_mystery"))
    assert u.plan == "free"


def test_counters_reset_only_when_period_rolls(db):
    now = datetime.now(timezone.utc)
    # Stripe periods are whole-second unix timestamps; mirror that so the
    # stored start matches the value a prior _apply_subscription would write.
    start = datetime.fromtimestamp(int(now.timestamp()), tz=timezone.utc)
    u = _user(db, drafts_used_this_period=4, contacts_scanned_this_period=10,
              billing_period_start=start,
              billing_period_end=now + timedelta(days=30))
    # Same period start -> mid-period update must NOT wipe usage.
    billing_route._apply_subscription(u, _sub(start=start, status="active"))
    assert u.drafts_used_this_period == 4
    assert u.contacts_scanned_this_period == 10
    # New period start -> counters reset.
    new_start = now + timedelta(days=30)
    billing_route._apply_subscription(u, _sub(start=new_start))
    assert u.drafts_used_this_period == 0
    assert u.contacts_scanned_this_period == 0


def test_apply_subscription_leaves_paid_at_untouched(db):
    paid = datetime.now(timezone.utc) - timedelta(days=5)
    u = _user(db, paid_at=paid)
    before = u.paid_at  # post-refresh value (SQLite drops tzinfo)
    billing_route._apply_subscription(u, _sub())
    assert u.paid_at == before


# ── user resolution ──────────────────────────────────────────────────────────

def test_user_by_subscription_id_then_customer(db):
    u = _user(db, stripe_subscription_id="sub_xyz", stripe_customer_id="cus_9")
    assert billing_route._user_by_subscription(db, {"id": "sub_xyz"}).id == u.id
    # falls back to customer when sub id unknown
    assert billing_route._user_by_subscription(
        db, {"id": "sub_other", "customer": "cus_9"}).id == u.id
    assert billing_route._user_by_subscription(db, {"id": "nope"}) is None


# ── webhook end-to-end (SDK stubbed) ─────────────────────────────────────────

class _FakeRequest:
    def __init__(self, payload: bytes):
        self._payload = payload
        self.headers = {"stripe-signature": "sig"}

    async def body(self) -> bytes:
        return self._payload


def _run_webhook(db, event: dict, monkeypatch, *, sub_obj=None):
    """Drive stripe_webhook with construct_event + Subscription.retrieve
    stubbed. `sub_obj` is what _retrieve_subscription returns (checkout
    subscription mode)."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")

    fake_stripe = SimpleNamespace(
        Webhook=SimpleNamespace(construct_event=lambda payload, sig, secret: event))
    monkeypatch.setattr(billing_route, "_stripe", lambda: fake_stripe)
    monkeypatch.setattr(billing_route, "_retrieve_subscription",
                        lambda sub_id: sub_obj)

    req = _FakeRequest(json.dumps(event).encode())
    return asyncio.get_event_loop().run_until_complete(
        billing_route.stripe_webhook(req, db))


def test_checkout_subscription_mode_applies_plan(db, monkeypatch):
    u = _user(db)
    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_1", "mode": "subscription",
                                  "subscription": "sub_1",
                                  "client_reference_id": str(u.id),
                                  "customer": "cus_1"}}}
    _run_webhook(db, event, monkeypatch, sub_obj=_sub(price="price_pro"))
    db.refresh(u)
    assert u.plan == "pro"
    assert u.subscription_status == "active"
    assert u.paid_at is None  # subscription path never stamps the legacy unlock


def test_checkout_one_time_mode_stamps_paid_at(db, monkeypatch):
    u = _user(db)
    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_2", "mode": "payment",
                                  "client_reference_id": str(u.id),
                                  "customer": "cus_2"}}}
    _run_webhook(db, event, monkeypatch)
    db.refresh(u)
    assert u.paid_at is not None
    assert u.plan == "free"  # one-time unlock doesn't touch the metered plan


def test_subscription_updated_changes_plan(db, monkeypatch):
    u = _user(db, plan="starter", stripe_subscription_id="sub_1")
    event = {"type": "customer.subscription.updated",
             "data": {"object": _sub(sub_id="sub_1", price="price_pro")}}
    _run_webhook(db, event, monkeypatch)
    db.refresh(u)
    assert u.plan == "pro"


def test_subscription_deleted_downgrades_to_free(db, monkeypatch):
    u = _user(db, plan="pro", subscription_status="active",
              stripe_subscription_id="sub_1", stripe_price_id="price_pro",
              drafts_used_this_period=7, contacts_scanned_this_period=3)
    event = {"type": "customer.subscription.deleted",
             "data": {"object": {"id": "sub_1", "customer": "cus_1"}}}
    _run_webhook(db, event, monkeypatch)
    db.refresh(u)
    assert u.plan == "free"
    assert u.subscription_status == "canceled"
    assert u.stripe_subscription_id is None
    assert u.drafts_used_this_period == 0


def test_unknown_event_is_acked(db, monkeypatch):
    event = {"type": "invoice.paid", "data": {"object": {"id": "in_1"}}}
    resp = _run_webhook(db, event, monkeypatch)
    assert resp.status_code == 200
