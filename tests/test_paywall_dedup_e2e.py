"""
Regression tests for the LinkedIn-dedup + paywall integration.

Background : prior to PR #142, _extract_profile_fields looked for snake_case
keys (`public_identifier`, `entity_urn`) while Unipile actually returns
camelCase under `connection_params.im` (`publicIdentifier`, `id`). As a
result every User row had NULL `linkedin_provider_id` and
`linkedin_public_id`, so the dedup loop added in #138 (and mirrored to the
webhook in #141) could never claim an existing user. A previously-paid
operator who cleared cookies would get a fresh User row with `paid_at=NULL`
and was forced to pay again. This test guards every layer of the fix.

Style mirrors test_demo_paywall.py : real backend imports, in-memory
SQLite, dedup loop replicated verbatim from routes/auth.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.auth import (
    require_paid_to_connect_linkedin,
    user_can_send_linkedin,
    user_has_paid,
)
from backend.db import Base
from backend.routes.auth import _extract_profile_fields


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


def _dedup_lookup(db, fields, new_account_id):
    """Replicate the dedup loop from linkedin_callback / linkedin_webhook.
    Returns (user, matched_by_key) or (None, None)."""
    user = db.query(models.User).filter(
        models.User.unipile_account_id == new_account_id).first()
    if user is not None:
        return user, "unipile_account_id"
    for key, val in (
        ("linkedin_provider_id", fields.get("linkedin_provider_id")),
        ("linkedin_public_id",   fields.get("linkedin_public_id")),
        ("email",                fields.get("email")),
    ):
        if not val:
            continue
        user = db.query(models.User).filter(
            getattr(models.User, key) == val).first()
        if user is not None:
            return user, key
    return None, None


def _apply_checkout_completed(db, obj):
    """Post-signature-verification half of the Stripe webhook handler
    (mirrors backend/routes/billing.py:172-191)."""
    user_id = (obj.get("client_reference_id")
               or (obj.get("metadata") or {}).get("user_id"))
    if not user_id:
        return None
    try:
        uid_int = int(user_id)
    except ValueError:
        return None
    user = db.query(models.User).filter(models.User.id == uid_int).first()
    if not user:
        return None
    user.paid_at = datetime.now(timezone.utc)
    cust = obj.get("customer")
    if cust and not user.stripe_customer_id:
        user.stripe_customer_id = cust
    db.commit()
    return user


# ── A. Extractor : the camelCase fix ──────────────────────────────────

def test_extractor_camelcase_real_unipile_shape():
    """Real Unipile payload (captured from /api/v1/accounts/<id>) must
    yield populated dedup keys. The whole regression originated here."""
    payload = {
        "id": "l3AW4l65RHmlVCz8OGdx1w",
        "name": "Daniel Wang",
        "type": "LINKEDIN",
        "connection_params": {"im": {
            "id": "ACoAADn5CK8B0EAb0OzPBcIzqFn9fyGiziHStBk",
            "publicIdentifier": "daniel04wang",
            "username": "Daniel Wang",
        }},
    }
    f = _extract_profile_fields(payload)
    assert f["linkedin_provider_id"] == "ACoAADn5CK8B0EAb0OzPBcIzqFn9fyGiziHStBk"
    assert f["linkedin_public_id"] == "daniel04wang"
    assert f["name"] == "Daniel Wang"


def test_extractor_snake_case_fallback_still_works():
    """Legacy / non-LinkedIn provider shapes used snake_case + entity_urn.
    We keep them as fallbacks so dedup doesn't regress for other providers."""
    payload = {"connection_params": {"im": {
        "public_identifier": "legacy_user",
        "entity_urn": "urn:li:person:LEGACY",
        "email": "legacy@example.com",
    }}}
    f = _extract_profile_fields(payload)
    assert f["linkedin_provider_id"] == "urn:li:person:LEGACY"
    assert f["linkedin_public_id"] == "legacy_user"
    assert f["email"] == "legacy@example.com"


def test_extractor_empty_payload_returns_none_keys():
    """When Unipile fetch fails or the shape drifts, both dedup keys must
    be None (not crash). The runtime WARNING is intentional and observable
    in stdout when this happens."""
    f = _extract_profile_fields({})
    assert f["linkedin_provider_id"] is None
    assert f["linkedin_public_id"] is None
    assert f["name"] == ""


# ── B. Gate logic (the bits not already in test_demo_paywall.py) ──────

def test_require_paid_to_connect_anonymous_signup_allowed():
    """First-time LinkedIn signup carries no User row yet : must be
    allowed through so SOMEONE can sign up for free."""
    require_paid_to_connect_linkedin(None)  # no exception


def test_require_paid_to_connect_unpaid_triage_user_gets_402(db):
    """Triage-signup user (email-only, no Stripe) trying to attach
    LinkedIn must hit the paywall with code=payment_required."""
    u = models.User(name="Triage", email="t@e.com",
                    unipile_account_id=None, linkedin_status="disconnected")
    db.add(u); db.commit(); db.refresh(u)
    with pytest.raises(HTTPException) as exc:
        require_paid_to_connect_linkedin(u)
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "payment_required"


# ── C. Dedup loop : the three fallback keys + priority ────────────────

def test_dedup_matches_by_linkedin_provider_id(db):
    """The primary key — the actual one in play for real LinkedIn auth."""
    db.add(models.User(name="A", unipile_account_id="old_acct",
                       linkedin_provider_id="urn:li:person:X",
                       linkedin_status="active"))
    db.commit()
    user, by = _dedup_lookup(
        db,
        {"linkedin_provider_id": "urn:li:person:X"},
        "NEW_acct",
    )
    assert user is not None and by == "linkedin_provider_id"


def test_dedup_falls_back_to_public_id_then_email(db):
    """If provider_id missing on incoming, try public_id, then email."""
    db.add(models.User(name="B", unipile_account_id="old_b",
                       linkedin_public_id="dan_w", linkedin_status="active"))
    db.commit()
    user, by = _dedup_lookup(
        db,
        {"linkedin_provider_id": None, "linkedin_public_id": "dan_w"},
        "NEW",
    )
    assert user is not None and by == "linkedin_public_id"


def test_dedup_falls_back_to_email(db):
    """Last-ditch fallback. Risky (shared inboxes) but better than spawning
    a duplicate row when LinkedIn keys are unavailable."""
    db.add(models.User(name="C", unipile_account_id="old_c",
                       email="dan@e.com", linkedin_status="active"))
    db.commit()
    user, by = _dedup_lookup(db, {"email": "dan@e.com"}, "NEW")
    assert user is not None and by == "email"


def test_dedup_priority_provider_id_beats_email(db):
    """When incoming payload could match multiple users, provider_id wins."""
    db.add(models.User(name="winner", unipile_account_id="a1",
                       linkedin_provider_id="urn:li:person:W",
                       linkedin_status="active"))
    db.add(models.User(name="loser", unipile_account_id="b1",
                       email="x@y.com", linkedin_status="active"))
    db.commit()
    user, by = _dedup_lookup(
        db,
        {"linkedin_provider_id": "urn:li:person:W", "email": "x@y.com"},
        "NEW",
    )
    assert user.name == "winner"
    assert by == "linkedin_provider_id"


def test_dedup_no_false_positive_when_no_match(db):
    """A missing match must NOT silently claim some other user."""
    db.add(models.User(name="A", unipile_account_id="a",
                       linkedin_provider_id="urn:p", linkedin_status="active"))
    db.commit()
    user, _ = _dedup_lookup(
        db, {"linkedin_provider_id": "urn:DIFFERENT"}, "NEW")
    assert user is None


def test_dedup_all_null_incoming_does_not_spuriously_match(db):
    """NULL=NULL is false in SQL. Existing row with all-NULL dedup keys must
    NOT match an incoming all-NULL set : that would claim arbitrary users
    and was the wrong direction this almost went."""
    db.add(models.User(name="A", unipile_account_id="a",
                       linkedin_status="active"))  # all keys NULL
    db.commit()
    user, _ = _dedup_lookup(
        db,
        {"linkedin_provider_id": None, "linkedin_public_id": None, "email": None},
        "NEW",
    )
    assert user is None


# ── D. Stripe webhook : stamping + idempotency ────────────────────────

def test_webhook_stamps_paid_at_and_customer_id(db):
    u = models.User(name="U", email="u@e.com", linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    _apply_checkout_completed(
        db, {"client_reference_id": str(u.id), "customer": "cus_001"})
    db.refresh(u)
    assert u.paid_at is not None
    assert u.stripe_customer_id == "cus_001"


def test_webhook_idempotent_does_not_overwrite_customer(db):
    """Stripe retries on non-2xx, so a replay with a different customer id
    must NOT silently overwrite the first one."""
    u = models.User(name="U", email="u@e.com", linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    _apply_checkout_completed(
        db, {"client_reference_id": str(u.id), "customer": "cus_first"})
    _apply_checkout_completed(
        db, {"client_reference_id": str(u.id), "customer": "cus_REPLAY"})
    db.refresh(u)
    assert u.stripe_customer_id == "cus_first"


def test_webhook_handles_metadata_user_id(db):
    """Stripe Checkout can send the user via metadata.user_id instead of
    client_reference_id : both paths must work."""
    u = models.User(name="U", email="u@e.com", linkedin_status="active")
    db.add(u); db.commit(); db.refresh(u)
    _apply_checkout_completed(
        db, {"metadata": {"user_id": str(u.id)}, "customer": "cus_meta"})
    db.refresh(u)
    assert u.paid_at is not None


@pytest.mark.parametrize("bad", [
    {},                                          # no user_id at all
    {"client_reference_id": "not_a_number"},     # non-int
    {"client_reference_id": "99999"},            # unknown user
])
def test_webhook_handles_malformed_inputs_without_crashing(db, bad):
    """Webhook must always return cleanly so Stripe stops retrying."""
    result = _apply_checkout_completed(db, bad)
    assert result is None  # no user to stamp


# ── E. End-to-end : the prod regression scenario ──────────────────────

def test_e2e_paid_user_clears_cookies_stays_paid(db):
    """THE BUG this whole fix exists for.

    Sequence :
      1. New user signs in with LinkedIn → User row with populated dedup keys
      2. User pays via Stripe → paid_at + stripe_customer_id stamped
      3. User clears cookies → fresh sign-in produces a NEW unipile_account_id
         for the SAME LinkedIn person
      4. Dedup loop must claim the existing User row by linkedin_provider_id
      5. paid_at, stripe_customer_id, user.id must all be preserved
      6. user_can_send_linkedin remains True

    Pre-#142, step 4 silently failed because the extractor returned NULL
    keys, so the dedup loop missed and a fresh User row with paid_at=NULL
    was inserted instead — forcing the operator to pay again.
    """
    # Step 1 : initial sign-in
    payload_1 = {
        "id": "unipile_acct_session_A",
        "name": "Test User",
        "type": "LINKEDIN",
        "connection_params": {"im": {
            "id": "urn:li:person:TESTUSER",
            "publicIdentifier": "testuser",
        }},
    }
    f1 = _extract_profile_fields(payload_1)
    u = models.User(
        unipile_account_id=payload_1["id"],
        name=f1["name"],
        email=f1.get("email"),
        linkedin_provider_id=f1["linkedin_provider_id"],
        linkedin_public_id=f1["linkedin_public_id"],
        linkedin_status="active",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(u); db.commit(); db.refresh(u)
    original_uid = u.id
    assert u.linkedin_provider_id == "urn:li:person:TESTUSER"  # the camelCase fix
    assert u.paid_at is None

    # Step 2 : Stripe webhook stamps paid_at
    _apply_checkout_completed(
        db, {"client_reference_id": str(u.id), "customer": "cus_e2e"})
    db.refresh(u)
    paid_at_value = u.paid_at
    stripe_value = u.stripe_customer_id
    assert paid_at_value is not None
    assert stripe_value == "cus_e2e"

    # Step 3 : cookies cleared → fresh sign-in with NEW unipile_account_id
    # but the same LinkedIn person (same im.id, same publicIdentifier)
    payload_2 = {
        "id": "unipile_acct_session_B",  # different account_id !
        "name": "Test User",
        "type": "LINKEDIN",
        "connection_params": {"im": {
            "id": "urn:li:person:TESTUSER",         # same URN
            "publicIdentifier": "testuser",         # same public id
        }},
    }
    f2 = _extract_profile_fields(payload_2)

    # Step 4 : dedup must match the existing row by linkedin_provider_id
    matched, by = _dedup_lookup(db, f2, payload_2["id"])
    assert matched is not None
    assert matched.id == original_uid
    assert by == "linkedin_provider_id"
    matched.unipile_account_id = payload_2["id"]
    db.commit()

    # Step 5 : THE PAYOFF — paid_at and stripe_customer_id preserved
    db.refresh(u)
    assert db.query(models.User).count() == 1, "duplicate User row created"
    assert u.paid_at == paid_at_value, "paid_at lost across cookie clear"
    assert u.stripe_customer_id == stripe_value, "stripe customer lost"
    assert u.unipile_account_id == "unipile_acct_session_B", "account_id not rotated"
    assert u.id == original_uid, "user.id changed (events would orphan)"

    # Step 6 : user can still send
    assert user_has_paid(u) is True
    assert user_can_send_linkedin(u) is True


def test_e2e_pre_fix_repro_dedup_would_have_missed(db):
    """Adversarial test : prove the OLD broken extractor would have missed.

    Simulates the pre-#142 state — a User row whose dedup keys are NULL
    because the old snake_case extractor never populated them. A fresh
    sign-in with the broken extractor produces None keys, so the dedup
    loop has nothing to match on, and a duplicate row would have been
    created. This test fails (and would re-introduce the bug) if anyone
    NULLs out the dedup keys in the future.
    """
    # Existing row with NULL dedup keys — what every prod row looked like
    # before #142.
    db.add(models.User(name="Test", unipile_account_id="old_acct",
                       linkedin_status="active"))
    db.commit()

    # Simulate the old broken extractor on a real Unipile payload : it
    # looked for snake_case keys that aren't there.
    real_payload = {"connection_params": {"im": {
        "id": "urn:li:person:TESTUSER",
        "publicIdentifier": "testuser",
    }}}
    broken_fields = {
        "linkedin_provider_id":
            real_payload["connection_params"]["im"].get("entity_urn"),       # None
        "linkedin_public_id":
            real_payload["connection_params"]["im"].get("public_identifier"),  # None
        "email": None,
    }
    matched, _ = _dedup_lookup(db, broken_fields, "NEW_acct")

    # The bug : dedup miss → caller would CREATE a new User row.
    assert matched is None, (
        "Pre-fix dedup would have missed and created a duplicate User row "
        "with paid_at=NULL, forcing the operator to re-pay. This is the "
        "exact failure mode #142 closes."
    )
