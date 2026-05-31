"""
Route tests for the in-person scan-to-connect entry point (routes/inperson.py)
and the shared warm/cold send helper (agents/send_flow.py).

Follows the repo convention : call the route functions directly with an
in-memory SQLAlchemy session + real User/Event rows (no TestClient/auth
cookies). Everything runs in UNIPILE_DRY_RUN so nothing touches the network.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.providers import reset_provider_cache


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "operator_acct")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # compose -> template
    monkeypatch.delenv("SURPLUS_KILL_OUTREACH", raising=False)
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
    """A fully connected + paid user so the send gate passes."""
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
        InPersonEventIn(label="NYC Tech Week — Founders Inc mixer", city="New York"),
        db, user)
    return out


# ── /events : create-or-fetch idempotency ──────────────────────────────────

def test_create_inperson_event_is_idempotent(db, user):
    first = _make_event(db, user)
    assert first["created"] is True
    assert first["event_id"]
    second = _make_event(db, user)
    assert second["created"] is False
    assert second["event_id"] == first["event_id"]
    # Exactly one Event row, and it's an in_person event needing only label+city.
    rows = db.query(models.Event).filter_by(kind="in_person").all()
    assert len(rows) == 1
    assert rows[0].label == "NYC Tech Week — Founders Inc mixer"
    assert rows[0].role == "" and rows[0].headcount == 0  # planning fields defaulted


# ── /resolve : resolve-only, never creates a Prospect ──────────────────────

def test_resolve_url_high_confidence(db, user):
    from backend.routes.inperson import resolve_identity, ResolveIn
    out = resolve_identity(
        ResolveIn(method="url",
                  linkedin_url="https://www.linkedin.com/in/maya-rodriguez?utm_source=share_via"),
        db, user)
    assert out["resolved"] is True
    cand = out["candidate"]
    assert cand["linkedin_url"] == "https://www.linkedin.com/in/maya-rodriguez"
    assert cand["provider_id"] == "dry_li_maya-rodriguez"
    assert cand["confidence"] == "high"
    # HARD RULE: resolving never creates a Prospect.
    assert db.query(models.Prospect).count() == 0


def test_resolve_text_without_exa_returns_empty_and_creates_nothing(db, user):
    from backend.routes.inperson import resolve_identity, ResolveIn
    out = resolve_identity(
        ResolveIn(method="text", name="Maya Rodriguez", title="Eng", company="Acme"),
        db, user)
    assert out["method"] == "text"
    assert out["count"] == 0 and out["candidates"] == []
    # HARD RULE: free text never becomes a Prospect on its own.
    assert db.query(models.Prospect).count() == 0


def test_resolve_bad_method_422(db, user):
    from fastapi import HTTPException
    from backend.routes.inperson import resolve_identity, ResolveIn
    with pytest.raises(HTTPException) as ei:
        resolve_identity(ResolveIn(method="carrier-pigeon"), db, user)
    assert ei.value.status_code == 422


# ── /scan : upsert pending Prospect + draft ────────────────────────────────

def test_scan_creates_pending_capture_with_draft(db, user):
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    out = scan_capture(
        ScanIn(event_id=ev["event_id"],
               linkedin_url="https://www.linkedin.com/in/maya-rodriguez/",
               source="scan", note="great chat at the booth",
               name="Maya Rodriguez"),
        db, user)
    assert out["resolve_failed"] is False
    assert out["draft_note"] and out["draft_message"]
    # The in-person warm framing references the event label.
    assert "NYC Tech Week" in out["draft_message"]

    p = db.query(models.Prospect).one()
    assert p.status == "pending"
    assert p.source == "scan"
    assert p.note == "great chat at the booth"
    assert p.captured_at is not None
    assert p.linkedin_provider_id == "dry_li_maya-rodriguez"
    assert p.linkedin_url == "https://www.linkedin.com/in/maya-rodriguez"


def test_scan_is_upsert_no_duplicate(db, user):
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    body = ScanIn(event_id=ev["event_id"],
                  linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                  source="link")
    scan_capture(body, db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="link", note="second pass"), db, user)
    rows = db.query(models.Prospect).all()
    assert len(rows) == 1
    assert rows[0].note == "second pass"   # refreshed, not duplicated


def test_scan_resolve_failure_stores_pending_and_flags(db, user, monkeypatch):
    from backend.providers.unipile import UnipileProvider
    from backend.routes.inperson import scan_capture, ScanIn

    def _boom(self, url):
        raise RuntimeError("unipile down")
    monkeypatch.setattr(UnipileProvider, "resolve_linkedin_user", _boom)

    ev = _make_event(db, user)
    out = scan_capture(
        ScanIn(event_id=ev["event_id"],
               linkedin_url="https://www.linkedin.com/in/ghosted",
               source="scan"),
        db, user)
    assert out["resolve_failed"] is True              # never 500
    p = db.query(models.Prospect).one()
    assert p.status == "pending"
    assert p.linkedin_provider_id is None
    assert out["prospect"]["resolve_failed"] is True


def test_scan_rejects_unowned_event(db, user):
    from fastapi import HTTPException
    from backend.routes.inperson import scan_capture, ScanIn
    other = models.User(name="Other", unipile_account_id="x")
    db.add(other); db.commit()
    ev = _make_event(db, other)   # owned by someone else
    with pytest.raises(HTTPException) as ei:
        scan_capture(ScanIn(event_id=ev["event_id"],
                            linkedin_url="https://www.linkedin.com/in/maya",
                            source="scan"), db, user)
    assert ei.value.status_code == 404


# ── /captures : CRM view ───────────────────────────────────────────────────

def test_list_captures_returns_crm_rows(db, user):
    from backend.routes.inperson import scan_capture, ScanIn, list_captures
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    out = list_captures(ev["event_id"], db, user)
    assert out["count"] == 1
    row = out["captures"][0]
    assert row["name"] == "Maya Rodriguez"
    assert row["status"] == "pending"
    assert row["connection_status"] == "unknown"
    assert row["resolve_failed"] is False
    assert row["last_outreach"] is None        # not sent yet
    assert row["conversion"] is None


# ── /send : shared warm/cold helper, dry-run ───────────────────────────────

def test_send_capture_dry_run_cold_path(db, user):
    from backend.routes.inperson import scan_capture, ScanIn, send_capture, SendIn
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    p = db.query(models.Prospect).one()

    out = send_capture(p.id, SendIn(note="see you next week!"), db, user)
    # Dry-run treats everyone as cold (is_relation False) -> send_connection.
    assert out["path_taken"] == "cold"
    assert out["dry_run"] is True
    assert out["state"] == "dry_run_queued"
    assert out["note_preview"] == "see you next week!"   # override honored
    # Dry-run must NOT flip status (mirrors /invite).
    db.refresh(p)
    assert p.status == "pending"
    # An OutreachLog row was written.
    assert any(o.channel == "linkedin" for o in p.outreach)


def test_send_capture_rejects_unowned(db, user):
    from fastapi import HTTPException
    from backend.routes.inperson import send_capture, SendIn
    other = models.User(name="Other", unipile_account_id="x")
    db.add(other); db.flush()
    ev = models.Event(user_id=other.id, kind="in_person", label="theirs", city="LA")
    db.add(ev); db.flush()
    p = models.Prospect(event_id=ev.id, identity="z", name="Z",
                        linkedin_url="https://www.linkedin.com/in/z")
    db.add(p); db.commit()
    with pytest.raises(HTTPException) as ei:
        send_capture(p.id, SendIn(), db, user)
    assert ei.value.status_code == 404


# ── shared helper directly : warm vs cold routing ──────────────────────────

def test_route_and_send_warm_path_uses_send_message(db, user, monkeypatch):
    from backend.providers.unipile import UnipileProvider
    from backend.agents.send_flow import route_and_send
    from backend.routes.inperson import scan_capture, ScanIn
    # Force a warm relation.
    monkeypatch.setattr(UnipileProvider, "is_relation", lambda self, url: True)

    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    p = db.query(models.Prospect).one()
    provider = UnipileProvider(dry_run=True, account_id="user_acct")

    outcome = route_and_send(db, p, provider, p.event)
    assert outcome.path_taken == "warm"
    assert outcome.connection_status == "connected"
    # Warm path sends a DM : the dry-run chat payload carries the message text.
    assert outcome.res.payload.get("text") == outcome.final_message
