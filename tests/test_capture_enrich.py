"""
Prompt-5 capture enrichment (agents/capture_enrich.py) + its wiring into
/api/inperson/scan : a fresh capture should land with a real name (and title /
firm when derivable), never "Unknown" / a bare handle, with or without
ANTHROPIC_API_KEY.

Follows the repo convention : route functions called directly with an
in-memory SQLAlchemy session, UNIPILE_DRY_RUN on, no network.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents import capture_enrich
from backend.db import Base
from backend.providers import reset_provider_cache


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture
def db(env):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    u = models.User(
        name="Operator", email="op@example.com",
        unipile_account_id="user_acct", linkedin_status="active",
        paid_at=datetime.now(timezone.utc),
    )
    db.add(u); db.commit()
    return u


def _make_event(db, user):
    from backend.routes.inperson import create_or_fetch_inperson_event, InPersonEventIn
    out = create_or_fetch_inperson_event(
        InPersonEventIn(label="NYC Tech Week"), db=db, user=user)
    return db.get(models.Event, out["event_id"])


def _scan(db, user, ev, url, **kw):
    from backend.routes.inperson import scan_capture, ScanIn
    return scan_capture(
        ScanIn(event_id=ev.id, linkedin_url=url, source="scan", **kw),
        db=db, user=user)


# ── handle heuristic ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("handle,expected", [
    ("satya-nadella", "Satya Nadella"),
    ("satya-nadella-1a2b3c", "Satya Nadella"),       # dedup suffix dropped
    ("maya-rodriguez-92", "Maya Rodriguez"),
    ("satyanadella", None),                          # unsplittable: no guess
    ("acme-solutions-inc-12345", "Acme Solutions Inc"),
    ("", None),
])
def test_name_from_handle(handle, expected):
    assert capture_enrich.name_from_handle(handle) == expected


# ── apply is fill-only ───────────────────────────────────────────────────────

def test_apply_record_fills_placeholders_only():
    p = models.Prospect(
        event_id=1, identity="x", name="satya-nadella",
        linkedin_url="https://www.linkedin.com/in/satya-nadella",
        role="Unknown", company="Unknown")
    rec = {"name": "Satya Nadella", "title": "CEO", "firm": "Microsoft",
           "email": "satya@microsoft.com"}
    assert capture_enrich.apply_record(p, rec) is True
    assert p.name == "Satya Nadella"
    assert p.role == "CEO"
    assert p.headline == "CEO"          # the Book titles people from headline
    assert p.company == "Microsoft"
    assert p.email == "satya@microsoft.com"


def test_apply_record_never_overwrites_operator_input():
    p = models.Prospect(
        event_id=1, identity="x", name="Satya N. (met at booth)",
        linkedin_url="https://www.linkedin.com/in/satya-nadella",
        role="Chief Executive", company="MSFT", email="real@msft.com",
        headline="CEO @ Microsoft")
    rec = {"name": "Wrong Name", "title": "CTO", "firm": "Other Corp",
           "email": "other@x.com"}
    assert capture_enrich.apply_record(p, rec) is False
    assert p.name == "Satya N. (met at booth)"
    assert p.role == "Chief Executive"
    assert p.company == "MSFT"
    assert p.email == "real@msft.com"
    assert p.headline == "CEO @ Microsoft"


# ── scan wiring : no key -> heuristic still beats "Unknown" ──────────────────

def test_scan_enriches_name_from_handle_without_key(db, user):
    ev = _make_event(db, user)
    out = _scan(db, user, ev, "https://www.linkedin.com/in/maya-rodriguez")
    assert out["prospect"]["name"] == "Maya Rodriguez"
    # The spine Contact picked up the enriched name too.
    p = db.get(models.Prospect, out["prospect"]["prospect_id"])
    contact = db.get(models.Contact, p.contact_id)
    assert contact.name == "Maya Rodriguez"


def test_scan_keeps_operator_name(db, user):
    ev = _make_event(db, user)
    out = _scan(db, user, ev, "https://www.linkedin.com/in/maya-rodriguez",
                name="Maya R.", role="Founder", company="Jetzy")
    assert out["prospect"]["name"] == "Maya R."
    assert out["prospect"]["role"] == "Founder"
    assert out["prospect"]["company"] == "Jetzy"


# ── scan wiring : LLM record applied when available ──────────────────────────

def test_scan_applies_llm_record(db, user, monkeypatch):
    monkeypatch.setattr(
        capture_enrich, "build_record",
        lambda p, e: {"name": "Satya Nadella", "title": "CEO",
                      "firm": "Microsoft", "linkedin": p.linkedin_url,
                      "email": None, "met_at": "NYC Tech Week"})
    ev = _make_event(db, user)
    out = _scan(db, user, ev, "https://www.linkedin.com/in/satyanadella")
    row = out["prospect"]
    assert row["name"] == "Satya Nadella"
    assert row["role"] == "CEO"
    assert row["company"] == "Microsoft"


def test_scan_survives_enrichment_crash(db, user, monkeypatch):
    def _boom(p, e):
        raise RuntimeError("llm exploded")
    monkeypatch.setattr(capture_enrich, "build_record", _boom)
    ev = _make_event(db, user)
    out = _scan(db, user, ev, "https://www.linkedin.com/in/satyanadella")
    assert out["prospect"]["name"] == "satyanadella"   # capture still lands


# ── contact back-fill on re-scan ─────────────────────────────────────────────

def test_rescan_backfills_placeholder_contact(db, user, monkeypatch):
    ev = _make_event(db, user)
    # First capture pre-enrichment : force a no-op record.
    monkeypatch.setattr(capture_enrich, "build_record",
                        lambda p, e: {"name": None, "title": None, "firm": None,
                                      "linkedin": None, "email": None, "met_at": ""})
    out1 = _scan(db, user, ev, "https://www.linkedin.com/in/satyanadella")
    p1 = db.get(models.Prospect, out1["prospect"]["prospect_id"])
    contact = db.get(models.Contact, p1.contact_id)
    assert contact.name == "satyanadella"              # placeholder contact
    # Re-scan with enrichment working.
    monkeypatch.setattr(
        capture_enrich, "build_record",
        lambda p, e: {"name": "Satya Nadella", "title": "CEO",
                      "firm": "Microsoft", "linkedin": None, "email": None,
                      "met_at": "NYC Tech Week"})
    _scan(db, user, ev, "https://www.linkedin.com/in/satyanadella")
    db.refresh(contact)
    assert contact.name == "Satya Nadella"
    assert contact.company == "Microsoft"


# ── the Book never shows "Unknown" for an enriched capture ───────────────────

def test_book_row_shows_enriched_capture(db, user, monkeypatch):
    monkeypatch.setattr(
        capture_enrich, "build_record",
        lambda p, e: {"name": "Satya Nadella", "title": "CEO",
                      "firm": "Microsoft", "linkedin": None, "email": None,
                      "met_at": "NYC Tech Week"})
    ev = _make_event(db, user)
    _scan(db, user, ev, "https://www.linkedin.com/in/satyanadella")

    from backend.routes.book import _book_from_spine
    rows = _book_from_spine(db, user)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Satya Nadella"
    assert row["title"] == "CEO"
    assert row["firm"] == "Microsoft"
    assert row["met_at"] == "NYC Tech Week"
    assert "Unknown" not in (row["name"], row["title"], row["firm"])
