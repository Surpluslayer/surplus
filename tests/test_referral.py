"""
Referral flywheel: attribution, first-win gate, qualify → hold → vest, clawback,
and the fraud gates. These pin the money path, so they exercise real state
transitions on an in-memory DB rather than mocking the module out.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, referral, billing_plans
from backend.db import Base


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("REFERRAL_ENABLED", "true")
    monkeypatch.setenv("REFERRAL_HOLD_DAYS", "30")
    monkeypatch.setenv("REFERRAL_REWARD_MONTHS", "1")
    monkeypatch.delenv("SURPLUS_BILLING_DISABLED", raising=False)
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, *, name="U", email=None, plan="free", verified=True, demo=False,
          li=None):
    u = models.User(name=name, email=email, plan=plan, subscription_status=(
        "active" if plan != "free" else "free"), email_verified=verified,
        is_demo=demo, linkedin_provider_id=li)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _referrer(db):
    r = _user(db, name="Referrer", email="ref@firm.com", plan="pro")
    referral.ensure_referral_code(db, r)
    return r


# ─── code + attribution ──────────────────────────────────────────────────────

def test_code_is_stable_and_unique(db):
    r = _referrer(db)
    first = r.referral_code
    assert first and referral.ensure_referral_code(db, r) == first  # idempotent
    other = _user(db, name="Other")
    assert referral.ensure_referral_code(db, other) != first


def test_capture_binds_valid_code(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="new@firm.com")
    assert referral.capture_referral_code(db, referee, r.referral_code) is True
    assert referee.referred_by_code == r.referral_code


def test_capture_rejects_self_and_unknown(db):
    r = _referrer(db)
    assert referral.capture_referral_code(db, r, r.referral_code) is False  # self
    referee = _user(db, name="Referee")
    assert referral.capture_referral_code(db, referee, "nope1234") is False
    assert referee.referred_by_code is None


def test_capture_does_not_overwrite(db):
    r1 = _referrer(db)
    r2 = _user(db, name="R2", email="r2@firm.com", plan="pro")
    referral.ensure_referral_code(db, r2)
    referee = _user(db, name="Referee")
    referral.capture_referral_code(db, referee, r1.referral_code)
    referral.capture_referral_code(db, referee, r2.referral_code)  # first wins
    assert referee.referred_by_code == r1.referral_code


# ─── first-win / prompt gate ─────────────────────────────────────────────────

def test_first_win_only_for_paying_users(db):
    free = _user(db, name="Free", plan="free")
    assert referral.stamp_first_win(db, free) is False
    assert free.first_win_at is None

    paid = _user(db, name="Paid", plan="pro")
    assert referral.stamp_first_win(db, paid) is True
    assert paid.first_win_at is not None
    # idempotent
    assert referral.stamp_first_win(db, paid) is False


def test_should_prompt_gate(db):
    paid = _user(db, name="Paid", plan="pro")
    assert referral.should_prompt(paid) is False       # no win yet
    referral.stamp_first_win(db, paid)
    assert referral.should_prompt(paid) is True         # win + paid, not prompted
    referral.mark_prompted(db, paid)
    assert referral.should_prompt(paid) is False        # fires once


def test_demo_never_prompts(db):
    demo = _user(db, name="Demo", plan="pro", demo=True)
    demo.first_win_at = datetime.now(timezone.utc)
    db.commit()
    assert referral.should_prompt(demo) is False


# ─── qualify → hold → vest ───────────────────────────────────────────────────

def test_paid_referee_qualifies_and_holds(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro")
    referral.capture_referral_code(db, referee, r.referral_code)

    ref = referral.on_referee_paid(db, referee)
    assert ref is not None and ref.status == "qualified"
    assert ref.hold_until is not None
    # two-sided: referee gets their welcome comp month now
    assert billing_plans.comp_active(referee) is True
    # referrer NOT yet rewarded (still in hold)
    assert r.comp_until is None

    # idempotent: paying again doesn't open a second referral
    assert referral.on_referee_paid(db, referee).id == ref.id


def test_vest_grants_referrer_after_hold(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro")
    referral.capture_referral_code(db, referee, r.referral_code)
    ref = referral.on_referee_paid(db, referee)

    # Before hold clears: nothing vests.
    assert referral.vest_due_referrals(db) == 0
    assert db.get(models.User, r.id).comp_until is None

    # Simulate the hold elapsing.
    ref.hold_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert referral.vest_due_referrals(db) == 1
    r2 = db.get(models.User, r.id)
    assert r2.comp_until is not None and billing_plans.comp_active(r2)
    assert db.get(models.Referral, ref.id).status == "rewarded"
    # idempotent: no double grant
    assert referral.vest_due_referrals(db) == 0


def test_clawback_before_vest(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro")
    referral.capture_referral_code(db, referee, r.referral_code)
    ref = referral.on_referee_paid(db, referee)

    assert referral.claw_back_for_referee(db, referee, reason="cancel") == 1
    assert db.get(models.Referral, ref.id).status == "rejected"
    # even past the hold, a rejected referral never vests
    ref.hold_until = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    assert referral.vest_due_referrals(db) == 0
    assert db.get(models.User, r.id).comp_until is None


# ─── fraud gates ─────────────────────────────────────────────────────────────

def test_unverified_referee_rejected(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro",
                    verified=False)
    referral.capture_referral_code(db, referee, r.referral_code)
    ref = referral.on_referee_paid(db, referee)
    assert ref is not None and ref.status == "rejected" and ref.reason == "unverified"
    assert r.comp_until is None


def test_same_email_is_self_referral(db):
    r = _referrer(db)
    referee = _user(db, name="Referee", email="ref@firm.com", plan="pro")  # same email
    referee.referred_by_code = r.referral_code  # force past capture's self guard
    db.commit()
    ref = referral.on_referee_paid(db, referee)
    assert ref.status == "rejected" and ref.reason == "self_referral"


def test_shared_linkedin_is_self_referral(db):
    r = _referrer(db)
    r.linkedin_provider_id = "ACoAA_same"; db.commit()
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro",
                    li="ACoAA_same")
    referee.referred_by_code = r.referral_code; db.commit()
    ref = referral.on_referee_paid(db, referee)
    assert ref.status == "rejected" and ref.reason == "self_referral"


def test_allowance_cap(db, monkeypatch):
    monkeypatch.setenv("REFERRAL_MAX_REWARDS_PER_YEAR", "1")
    r = _referrer(db)
    # one already rewarded this year
    db.add(models.Referral(referrer_id=r.id, referee_id=_user(db, name="A").id,
                           code=r.referral_code, status="rewarded",
                           rewarded_at=datetime.now(timezone.utc)))
    db.commit()
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro")
    referee.referred_by_code = r.referral_code; db.commit()
    ref = referral.on_referee_paid(db, referee)
    assert ref.status == "rejected" and ref.reason == "allowance_cap"


def test_disabled_flag_noops(db, monkeypatch):
    monkeypatch.setenv("REFERRAL_ENABLED", "false")
    r = _user(db, name="R", email="r@f.com", plan="pro")
    assert referral.ensure_referral_code(db, r) is not None  # code still works
    referee = _user(db, name="Referee", email="new@firm.com", plan="pro")
    assert referral.capture_referral_code(db, referee, "anything") is False
    assert referral.stamp_first_win(db, r) is False
    assert referral.should_prompt(r) is False
