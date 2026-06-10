"""
Unit tests for the LinkedInProvider abstraction + UnipileProvider.

These tests must NEVER hit the network. Dry-run paths in UnipileProvider
short-circuit before _lookup_provider_id / _post : tests patch both to
assert that explicitly.
"""
from __future__ import annotations
import hmac
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.providers import UnipileProvider, get_provider, reset_provider_cache
from backend.providers.base import (
    LeadPayload,
    ProviderResult,
    CanonicalEvent,
    CANONICAL_STATES,
)


@pytest.fixture(autouse=True)
def clear_provider_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture
def fake_prospect():
    return SimpleNamespace(
        id=42,
        identity="maya-rodriguez",
        name="Maya Rodriguez",
        role="Staff Infra Engineer",
        company="Lo91r",
        seniority="Staff+",
        side="Builds",
        works_on="observability",
        offers="Observability depth",
        seeks="Staff-scope role",
        gh_stars=2100,
        x_followers=4800,
        li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
        sources="github,linkedin,x",
        fit_score=88,
        fit_reason="strong open-source footprint; seniority meets target",
        status="approved",
    )


@pytest.fixture
def fake_event():
    return SimpleNamespace(
        id=1,
        role="Infrastructure / ML platform engineers",
        seniority="Senior",
        co_stage="Seed",
        headcount=9,
        format="Hackathon",
        city="San Francisco",
        goal="Hiring pipeline",
        budget=9000,
        threshold=78,
    )


# ===== payload building ====================================================

def test_build_lead_payload_splits_name_and_carries_internal_ids(fake_prospect, fake_event):
    provider = UnipileProvider(dry_run=True)
    lead = provider.build_lead_payload(
        fake_prospect, fake_event,
        note="Hi Maya : short note",
        message="Longer follow-up message.",
    )
    assert lead.event_id == 1
    assert lead.prospect_id == 42
    assert lead.first_name == "Maya"
    assert lead.last_name == "Rodriguez"
    assert lead.linkedin_url == "https://www.linkedin.com/in/maya-rodriguez"
    assert lead.note == "Hi Maya : short note"
    assert lead.message == "Longer follow-up message."
    assert lead.fit_score == 88


# ===== dry-run: NO network ================================================

def test_unipile_dry_run_send_connection_returns_fake_provider_id(fake_prospect, fake_event):
    p = UnipileProvider(dry_run=True, account_id="acct_test")
    lead = p.build_lead_payload(fake_prospect, fake_event, "the note", "the message")

    with patch.object(p, "_post", side_effect=AssertionError("DRY_RUN must not POST")), \
         patch.object(p, "_lookup_provider_id",
                      side_effect=AssertionError("DRY_RUN must not look up profile")):
        res = p.send_connection(lead)

    assert res.dry_run is True
    assert res.state == "dry_run_queued"
    assert res.state in CANONICAL_STATES
    assert res.provider == "unipile"
    assert res.provider_lead_id.startswith("dry_")
    assert res.linkedin_provider_id is not None
    assert res.linkedin_provider_id.startswith("dry_li_")
    assert res.payload["account_id"] == "acct_test"
    assert res.payload["message"] == "the note"


def test_unipile_dry_run_send_message_uses_provided_provider_id(fake_prospect, fake_event):
    p = UnipileProvider(dry_run=True, account_id="acct_test")
    lead = p.build_lead_payload(fake_prospect, fake_event, "n", "the DM body")
    res = p.send_message(lead, linkedin_provider_id="ACoAAA_real_id")
    assert res.state == "dry_run_queued"
    assert res.linkedin_provider_id == "ACoAAA_real_id"
    assert res.payload["attendees_ids"] == ["ACoAAA_real_id"]
    assert res.payload["text"] == "the DM body"


def test_unipile_live_send_refuses_without_creds(fake_prospect, fake_event):
    """Live mode missing creds should fail safely, not silently call out."""
    p = UnipileProvider(dry_run=False, api_key=None, dsn=None, account_id=None)
    lead = p.build_lead_payload(fake_prospect, fake_event, "n", "m")
    res = p.send_connection(lead)
    assert res.state == "failed"
    assert "UNIPILE_" in (res.error or "")


def test_unipile_live_send_message_refuses_without_provider_id(fake_prospect, fake_event):
    """Live send_message without a known LinkedIn provider_id should fail safely."""
    p = UnipileProvider(dry_run=False, api_key="k", dsn="https://x", account_id="a")
    lead = p.build_lead_payload(fake_prospect, fake_event, "n", "m")
    res = p.send_message(lead, linkedin_provider_id=None)
    assert res.state == "failed"
    assert "linkedin_provider_id" in (res.error or "")


# ===== webhook normalization ==============================================

def test_unipile_normalize_new_relation_event():
    p = UnipileProvider(dry_run=True)
    raw = {
        "event": "new_relation",
        "timestamp": "2026-05-14T12:00:00Z",
        "user_provider_id": "ACoAAA_xyz",
        "user": {"public_identifier": "maya-rodriguez"},
    }
    ev = p.normalize_webhook(raw)
    assert ev is not None
    assert ev.state == "invite_accepted"
    assert ev.provider == "unipile"
    assert ev.provider_lead_id == "ACoAAA_xyz"
    # Unipile doesn't embed back-pointers : route resolves these via DB
    assert ev.event_id == 0
    assert ev.prospect_id == 0


def test_unipile_normalize_new_message_event():
    p = UnipileProvider(dry_run=True)
    raw = {
        "event": "new_message",
        "user_provider_id": "ACoAAA_xyz",
        "message": {"text": "yes, sounds great"},
    }
    ev = p.normalize_webhook(raw)
    assert ev is not None
    assert ev.state == "message_replied"
    assert ev.body == "yes, sounds great"


def test_unipile_normalize_messaging_source_message_received():
    """Unipile's "messaging" webhook source emits `message_received` (not
    `new_message`) and nests the sender's provider_id under `sender`. Both
    must resolve, or auto-reply never matches a prospect."""
    p = UnipileProvider(dry_run=True)
    raw = {
        "event": "message_received",
        "chat_id": "chat_abc",
        "sender": {"attendee_provider_id": "ACoAAA_sender"},
        "message": "are you still hiring?",
    }
    ev = p.normalize_webhook(raw)
    assert ev is not None
    assert ev.state == "message_replied"
    assert ev.provider_lead_id == "ACoAAA_sender"
    assert ev.body == "are you still hiring?"


def test_unipile_normalize_unknown_event_returns_none():
    p = UnipileProvider(dry_run=True)
    assert p.normalize_webhook({"event": "ufo_detected"}) is None
    assert p.normalize_webhook({}) is None
    assert p.normalize_webhook(None) is None  # type: ignore[arg-type]


# ===== webhook signature verification =====================================

def test_unipile_verify_webhook_passes_with_valid_hmac():
    secret = "test-secret-xyz"
    p = UnipileProvider(dry_run=True, webhook_secret=secret)
    body = b'{"event":"new_relation"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert p.verify_webhook({"x-unipile-signature": f"sha256={sig}"}, body) is True
    assert p.verify_webhook({"X-Signature": sig}, body) is True


def test_unipile_verify_webhook_fails_with_bad_signature():
    p = UnipileProvider(dry_run=True, webhook_secret="real-secret")
    body = b'{"x":1}'
    assert p.verify_webhook({"x-unipile-signature": "sha256=deadbeef"}, body) is False
    assert p.verify_webhook({}, body) is False


def test_unipile_verify_webhook_passes_with_static_secret_header():
    """Real Unipile traffic authenticates via a STATIC custom header (Unipile
    doesn't body-HMAC), so a matching X-Webhook-Secret must pass regardless of
    body."""
    secret = "static-shared-secret"
    p = UnipileProvider(dry_run=True, webhook_secret=secret)
    body = b'{"event":"message_received"}'
    assert p.verify_webhook({"X-Webhook-Secret": secret}, body) is True
    assert p.verify_webhook({"x-webhook-secret": secret}, body) is True
    # Wrong token still fails (and no HMAC sig present).
    assert p.verify_webhook({"X-Webhook-Secret": "nope"}, body) is False


def test_unipile_register_inbound_webhook_needs_creds():
    """Without DSN/API key (dry-run dev), registration declines cleanly rather
    than attempting a live HTTP call."""
    p = UnipileProvider(dry_run=True)
    out = p.register_inbound_webhook("https://x.test/webhooks/unipile")
    assert out["ok"] is False


def test_unipile_verify_webhook_no_secret_denies_in_prod_allows_in_dev():
    p = UnipileProvider(dry_run=True, webhook_secret=None, require_signature=True)
    assert p.verify_webhook({}, b"") is False

    p = UnipileProvider(dry_run=True, webhook_secret=None, require_signature=False)
    assert p.verify_webhook({}, b"") is True


# ===== env wiring + DRY_RUN default =======================================

def test_get_provider_defaults_to_dry_run(monkeypatch):
    """Critical gate: missing env var must result in DRY_RUN=true."""
    monkeypatch.delenv("UNIPILE_DRY_RUN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("PROVIDER", raising=False)
    reset_provider_cache()
    p = get_provider()
    assert p.name == "unipile"
    assert p.dry_run is True
    # KILL SWITCH (unipile.py:auto_dm_after_accept): post-accept auto-DM is
    # deliberately hard-off — it used to fire an unattended DM from the
    # host's LinkedIn on every invite_accepted. Pin the off state so a
    # refactor can't silently re-enable it.
    assert p.auto_dm_after_accept is False


def test_dry_run_must_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "false")
    reset_provider_cache()
    p = get_provider()
    assert p.dry_run is False


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("PROVIDER", "n8n")
    reset_provider_cache()
    with pytest.raises(ValueError, match="Unknown PROVIDER"):
        get_provider()
