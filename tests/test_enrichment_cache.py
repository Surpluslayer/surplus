"""tests/test_enrichment_cache.py : the cross-event identity enrichment cache.

Covers the Step-1 read-through cache: identity keying (strong-key-only, email
hash determinism, slug-first ordering), the freshness/TTL gate, the put/get
roundtrip across keys (write under email+slug, hit by EITHER), the quality guard
(never freeze empty / throttle-stripped pulls), the flag gate, and fail-soft on
corrupt entries.

The cache opens its OWN SessionLocal (independent of the caller's transaction),
so we monkeypatch backend.db.SessionLocal onto a StaticPool in-memory SQLite that
all connections share — otherwise each new connection would see an empty :memory:
DB. TRIAGE_ENRICH_CACHE is forced on per-test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.db as db_mod
from backend.db import Base
from backend import models  # noqa: F401  (registers TriageEnrichmentCache)
from backend.triage import enrichment_cache as ec
from backend.triage.enrich import RawEvidence, PersonEvidence, CompanyCandidate


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def cache_db(monkeypatch):
    """A shared in-memory SQLite the cache's own SessionLocal will use."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", TestingSession)
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE", "1")
    return TestingSession


def _raw(*, found=True, work_unreliable=False, headline="CEO @ Acme",
         profile_url="https://www.linkedin.com/in/janedoe") -> RawEvidence:
    p = PersonEvidence(
        found=found, profile_url=profile_url, headline=headline,
        work_experience_found=True, work_experience=["CEO @ Acme"],
        work_companies=["Acme"], work_unreliable=work_unreliable,
    )
    c = CompanyCandidate(name="Acme", source="linkedin_company",
                         website="https://acme.com")
    return RawEvidence(person=p, company_candidates=[c])


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


# ── freshness / TTL ─────────────────────────────────────────────────────────
def test_is_fresh_handles_naive_and_aware(monkeypatch):
    monkeypatch.delenv("TRIAGE_ENRICH_CACHE_TTL_DAYS", raising=False)
    now_aware = datetime.now(timezone.utc)
    assert ec._is_fresh(now_aware) is True
    # naive timestamp (SQLite-style) must not raise and is treated as UTC
    assert ec._is_fresh(datetime.utcnow()) is True
    assert ec._is_fresh(now_aware - timedelta(days=400)) is False
    assert ec._is_fresh(None) is False


# ── put / get roundtrip ─────────────────────────────────────────────────────
def test_put_then_get_by_same_key(cache_db):
    keys = ec.identity_keys(linkedin_url="https://www.linkedin.com/in/janedoe")
    ec.cache_put(keys, _raw())
    got = ec.cache_get(keys)
    assert got is not None
    assert got.person.found is True
    assert got.person.headline == "CEO @ Acme"
    assert got.company_candidates[0].name == "Acme"


def test_email_first_hit_when_only_email_known(cache_db):
    """Write under BOTH email+slug (as the scorer does post-fetch); a later event
    that knows ONLY the email must hit — this is the people-search-avoidance win."""
    write_keys = ec.identity_keys(
        email="jane@acme.com",
        linkedin_url="https://www.linkedin.com/in/janedoe")
    ec.cache_put(write_keys, _raw())
    # New event row: no LinkedIn URL pasted, only the email.
    read_keys = ec.identity_keys(email="jane@acme.com")
    got = ec.cache_get(read_keys)
    assert got is not None and got.person.found is True


def test_slug_hit_when_only_linkedin_known(cache_db):
    write_keys = ec.identity_keys(
        email="jane@acme.com",
        linkedin_url="https://www.linkedin.com/in/janedoe")
    ec.cache_put(write_keys, _raw())
    got = ec.cache_get(
        ec.identity_keys(linkedin_url="https://www.linkedin.com/in/janedoe"))
    assert got is not None and got.person.found is True


def test_miss_returns_none(cache_db):
    assert ec.cache_get(ec.identity_keys(email="nobody@nowhere.com")) is None


# ── quality guard ───────────────────────────────────────────────────────────
def test_does_not_cache_empty_pull(cache_db):
    keys = ec.identity_keys(email="empty@acme.com")
    ec.cache_put(keys, RawEvidence())  # is_empty() → skipped
    assert ec.cache_get(keys) is None


def test_does_not_cache_throttle_stripped(cache_db):
    keys = ec.identity_keys(email="stripped@acme.com")
    ec.cache_put(keys, _raw(work_unreliable=True))  # stripped → skipped
    assert ec.cache_get(keys) is None


# ── flag gate ────────────────────────────────────────────────────────────────
def test_disabled_flag_is_noop(cache_db, monkeypatch):
    keys = ec.identity_keys(email="jane@acme.com")
    ec.cache_put(keys, _raw())            # enabled by fixture
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE", "off")
    assert ec.cache_enabled() is False
    assert ec.cache_get(keys) is None      # get is a no-op when disabled
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE", "1")
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE", "0")
    ec.cache_put(ec.identity_keys(email="z@acme.com"), _raw())
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE", "1")
    assert ec.cache_get(ec.identity_keys(email="z@acme.com")) is None  # never written


# ── fail-soft ────────────────────────────────────────────────────────────────
def test_corrupt_entry_is_a_miss(cache_db):
    from backend.models import TriageEnrichmentCache
    key = ec.identity_keys(email="corrupt@acme.com")[0]
    s = cache_db()
    s.add(TriageEnrichmentCache(cache_key=key, evidence="{not json",
                                source="unipile",
                                fetched_at=datetime.now(timezone.utc)))
    s.commit(); s.close()
    assert ec.cache_get([key]) is None  # JSON error → miss, no raise


def test_stale_entry_is_a_miss(cache_db, monkeypatch):
    from backend.models import TriageEnrichmentCache
    monkeypatch.setenv("TRIAGE_ENRICH_CACHE_TTL_DAYS", "30")
    keys = ec.identity_keys(email="stale@acme.com")
    ec.cache_put(keys, _raw())
    # Backdate the row well past the TTL.
    s = cache_db()
    row = s.get(TriageEnrichmentCache, keys[0])
    row.fetched_at = datetime.now(timezone.utc) - timedelta(days=120)
    s.commit(); s.close()
    assert ec.cache_get(keys) is None
