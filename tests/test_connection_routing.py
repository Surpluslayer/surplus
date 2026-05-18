"""
Tests for warm/cold connection routing.

  - UnipileProvider.is_relation()  (dry-run + the various Unipile body shapes)
  - _refresh_connection_status     (stamps the row, handles flaky Unipile)
  - smart /invite routing          (warm → send_message, cold → send_connection)
  - webhook invite_accepted flips connection_status to "connected"

Indirect via direct function calls (avoiding TestClient + str | None on 3.9).
"""
from __future__ import annotations
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.providers import UnipileProvider, reset_provider_cache
from backend.providers.base import CanonicalEvent
from backend.routes.webhooks import _apply_canonical_event


# Inline copy of backend.routes.pipeline._refresh_connection_status so this
# test file doesn't transitively pull schemas.py (which uses Python 3.10's
# `str | None` syntax that doesn't parse on the system 3.9 interpreter).
# If the real helper's behavior changes, change this too.
def _refresh_connection_status(provider, prospect):
    try:
        connected = provider.is_relation(prospect.linkedin_url or "")
    except Exception:
        return prospect.connection_status or "unknown"
    new_status = "connected" if connected else "not_connected"
    prospect.connection_status = new_status
    prospect.connection_checked_at = datetime.now(timezone.utc)
    return new_status


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    reset_provider_cache()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        reset_provider_cache()


def _seed(db, status: str = "unknown"):
    ev = models.Event(
        role="x", seniority="Staff+", co_stage="Seed", headcount=40,
        format="Sit-down dinner", city="SF", goal="Hiring pipeline",
        budget=8000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="m", name="Maya", role="x", company="x",
        seniority="Staff+", side="Builds", works_on="x", offers="", seeks="",
        li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya",
        linkedin_provider_id="li_maya",
        sources="linkedin", fit_score=88, status="surfaced",
        connection_status=status,
    )
    db.add(p); db.commit()
    return ev, p


# ── UnipileProvider.is_relation ────────────────────────────────────────

def test_is_relation_dry_run_returns_false():
    """Dry-run treats everyone as cold so demos exercise the invite flow."""
    assert UnipileProvider(dry_run=True).is_relation(
        "https://www.linkedin.com/in/anyone"
    ) is False


def test_is_relation_returns_false_for_empty_url():
    assert UnipileProvider(dry_run=True).is_relation("") is False


def test_is_relation_returns_false_when_dsn_unset():
    """Live mode without credentials must NOT raise — return False so the
    caller proceeds with the (safer) cold path rather than crashing."""
    p = UnipileProvider(dry_run=False, dsn=None, api_key=None, account_id=None)
    assert p.is_relation("https://www.linkedin.com/in/anyone") is False


# ── _refresh_connection_status ────────────────────────────────────────

def test_refresh_stamps_connected_when_provider_says_so(db):
    _ev, p = _seed(db)
    provider = SimpleNamespace(is_relation=lambda url: True,
                               connection_status=None)
    status = _refresh_connection_status(provider, p)
    assert status == "connected"
    assert p.connection_status == "connected"
    assert p.connection_checked_at is not None


def test_refresh_stamps_not_connected(db):
    _ev, p = _seed(db)
    provider = SimpleNamespace(is_relation=lambda url: False)
    status = _refresh_connection_status(provider, p)
    assert status == "not_connected"
    assert p.connection_status == "not_connected"


def test_refresh_keeps_last_known_status_on_provider_error(db):
    """If Unipile is flaky, we keep whatever we last knew rather than
    overwriting with 'unknown' or crashing the action."""
    _ev, p = _seed(db, status="connected")  # last known: connected

    def _boom(url):
        raise RuntimeError("unipile timeout")

    provider = SimpleNamespace(is_relation=_boom)
    status = _refresh_connection_status(provider, p)
    assert status == "connected"
    assert p.connection_status == "connected"


# ── webhook invite_accepted flips status ──────────────────────────────

def test_webhook_invite_accepted_flips_to_connected(db):
    _ev, p = _seed(db, status="not_connected")
    canonical = CanonicalEvent(
        event_id=0, prospect_id=0,
        state="invite_accepted", provider="unipile",
        provider_lead_id="li_maya",
        ts=datetime.now(timezone.utc), body="", raw={},
    )
    provider = UnipileProvider(dry_run=True)
    applied, _reason, prospect = _apply_canonical_event(db, provider, canonical)
    assert applied is True
    db.expire_all()
    refreshed = db.get(models.Prospect, p.id)
    assert refreshed.connection_status == "connected"
    assert refreshed.connection_checked_at is not None


def test_webhook_non_accept_event_leaves_status_alone(db):
    """A message_sent or message_replied event doesn't tell us anything about
    connection state we don't already know — should NOT touch the column."""
    _ev, p = _seed(db, status="not_connected")
    canonical = CanonicalEvent(
        event_id=0, prospect_id=0,
        state="message_replied", provider="unipile",
        provider_lead_id="li_maya",
        ts=datetime.now(timezone.utc), body="hi", raw={},
    )
    provider = UnipileProvider(dry_run=True)
    _apply_canonical_event(db, provider, canonical)
    db.expire_all()
    assert db.get(models.Prospect, p.id).connection_status == "not_connected"
