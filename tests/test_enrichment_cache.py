"""tests/test_enrichment_cache.py : the identity-key kernel.

Covers identity keying (strong-key-only, email hash determinism, slug-first
ordering, phone-hash normalization) from
backend/agents/relationship/enrichment_cache.py — the kernel every
relationship-side sync path (email, LinkedIn chat, Google contacts, WhatsApp,
spine dedup) keys people by.

The DB read/write half of the old cross-event cache (cache_get/cache_put and
its TTL/quality/flag gates) was deleted with the events-side triage pipeline;
those tests went with it.
"""
from __future__ import annotations

from backend.agents.relationship import enrichment_cache as ec


# ── identity keying ─────────────────────────────────────────────────────────
def test_email_hash_is_deterministic_and_salted():
    h1 = ec._email_hash("Jane@Acme.com")
    h2 = ec._email_hash("jane@acme.com")  # case-insensitive
    assert h1 and h1 == h2
    assert ec._email_hash("jane@other.com") != h1  # different email → different key


def test_email_hash_rejects_garbage():
    for bad in ("", "   ", "noatsign", "@nodomain", "nolocal@"):
        assert ec._email_hash(bad) == ""


def test_identity_keys_slug_first_then_email():
    keys = ec.identity_keys(
        email="jane@acme.com",
        linkedin_url="https://www.linkedin.com/in/janedoe/")
    assert keys[0].startswith("li:")
    assert keys[1].startswith("em:")
    assert keys[0] == "li:janedoe"


def test_identity_keys_email_only_and_none():
    assert ec.identity_keys(email="jane@acme.com") == [
        "em:" + ec._email_hash("jane@acme.com")]
    # No strong signal → no keys (never key on a name).
    assert ec.identity_keys() == []
    assert ec.identity_keys(email="", linkedin_url="") == []


def test_phone_hash_normalizes_and_folds_country_code():
    # country-code'd and national forms hash the same (last 10 digits)
    assert ec._phone_hash("+1 (415) 555-1234") == ec._phone_hash("415-555-1234")
    assert ec._phone_hash("+1 415 555 1234") == ec._phone_hash("4155551234")
    # too short -> no key; namespaced apart from email hashes
    assert ec._phone_hash("555-1234") == ""
    assert ec._phone_hash("") == ""


def test_identity_keys_phone_for_whatsapp_contacts():
    # phone alone keys a WhatsApp/SMS contact (no email/LinkedIn)
    assert ec.identity_keys(phone="+1 415 555 1234") == [
        "ph:" + ec._phone_hash("4155551234")]
    # ordering: li > em > ph
    keys = ec.identity_keys(linkedin_url="https://www.linkedin.com/in/jane/",
                            email="jane@acme.com", phone="4155551234")
    assert [k[:3] for k in keys] == ["li:", "em:", "ph:"]
    # backward compatible: existing email/linkedin callers unaffected
    assert ec.identity_keys(email="jane@acme.com")[0].startswith("em:")


# ── LinkedIn slug parsing ───────────────────────────────────────────────────
def test_linkedin_slug_robust_to_suffixes():
    assert ec._linkedin_slug("https://www.linkedin.com/in/janedoe/") == "janedoe"
    assert ec._linkedin_slug("https://linkedin.com/in/yahal/en") == "yahal"
    assert ec._linkedin_slug("https://www.linkedin.com/in/janedoe?utm=x") == "janedoe"
    assert ec._linkedin_slug("") == ""
