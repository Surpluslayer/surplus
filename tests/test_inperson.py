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


def _req(host="event.surpluslayer.com"):
    """Minimal Starlette Request whose browser-host resolves to `host` (via the
    Origin header), for exercising send_capture's host-aware send gate."""
    from starlette.requests import Request
    headers = [(b"origin", f"https://{host}".encode())] if host else []
    return Request({"type": "http", "headers": headers, "method": "POST",
                    "path": "/api/inperson/captures/1/send"})


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

def test_update_scheduling_saves_and_normalizes(db, user):
    """The saved booking link + reply-to email persist on the user and a bare
    host is normalized to https://. Junk is rejected; empty clears."""
    import pytest as _pytest
    from fastapi import HTTPException
    from backend.routes.auth import update_scheduling, SchedulingBody

    out = update_scheduling(
        SchedulingBody(calendly_url="calendly.com/jane/15min", email="jane@x.com"),
        user, db)
    body = out.body.decode() if hasattr(out, "body") else ""
    db.refresh(user)
    assert user.calendly_url == "https://calendly.com/jane/15min"  # normalized
    assert user.email == "jane@x.com"
    assert "https://calendly.com/jane/15min" in body

    # Junk link -> 422
    with _pytest.raises(HTTPException) as ei:
        update_scheduling(SchedulingBody(calendly_url="notaurl"), user, db)
    assert ei.value.status_code == 422

    # Empty string clears it
    update_scheduling(SchedulingBody(calendly_url=""), user, db)
    db.refresh(user)
    assert user.calendly_url is None


def test_send_capture_dry_run_cold_path(db, user):
    from backend.routes.inperson import scan_capture, ScanIn, send_capture, SendIn
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    p = db.query(models.Prospect).one()

    out = send_capture(p.id, _req(), SendIn(note="see you next week!"), db, user)
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


def test_scan_repersonalizes_draft_and_stores_private_note(db, user):
    """The fun fact (`note`) must drive the composed draft, and re-scanning
    with an updated fact must re-personalize it. `private_note` is stored
    separately and never feeds the draft."""
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    url = "https://www.linkedin.com/in/maya-rodriguez"

    bare = scan_capture(ScanIn(event_id=ev["event_id"], linkedin_url=url,
                               source="scan", name="Maya"), db, user)
    catered = scan_capture(ScanIn(event_id=ev["event_id"], linkedin_url=url,
                                  source="scan", name="Maya",
                                  note="from Ottawa",
                                  private_note="intro to Dana"), db, user)

    # Re-scan with the fun fact changes the draft (re-personalized).
    assert catered["draft_note"] != bare["draft_note"]
    assert "Ottawa" in catered["draft_note"]
    # Fun fact + private note land on the row, separately.
    p = db.query(models.Prospect).one()
    assert p.note == "from Ottawa"
    assert p.private_note == "intro to Dana"
    # The serialized capture exposes both.
    assert catered["prospect"]["note"] == "from Ottawa"
    assert catered["prospect"]["private_note"] == "intro to Dana"


def test_next_step_woven_into_first_message_and_contact_type_stored(db, user):
    """An opt-in next step (e.g. a Calendly link) is woven into the first
    message; contact_type is persisted for later triage but never sent."""
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    r = scan_capture(ScanIn(
        event_id=ev["event_id"],
        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
        source="scan", name="Maya", note="from Ottawa",
        contact_type="sales",
        next_step="grab a coffee — book a time: calendly.com/me/15min",
    ), db, user)
    # The next step (with the link) shows up in the composed first message.
    assert "calendly.com/me/15min" in r["draft_message"]
    # Stored on the row + serialized, separate from the sent copy.
    p = db.query(models.Prospect).one()
    assert p.contact_type == "sales"
    assert p.next_step.startswith("grab a coffee")
    assert r["prospect"]["contact_type"] == "sales"
    # contact_type is operator metadata : it must NOT leak into the note/message.
    assert "sales" not in r["draft_note"].lower()


def test_send_no_note_sends_bare_invite(db, user):
    """`no_note` sends a bare invite (empty note) regardless of any note text,
    so it dodges LinkedIn's 300-char cap. The post-accept DM is unaffected."""
    from backend.routes.inperson import scan_capture, ScanIn, send_capture, SendIn
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya", note="from Ottawa"), db, user)
    p = db.query(models.Prospect).one()

    out = send_capture(p.id, _req(), SendIn(no_note=True, note="ignored"), db, user)
    assert out["path_taken"] == "cold"
    assert out["note_preview"] == ""        # bare : no note attached


def test_invite_payload_omits_message_when_note_blank(db, user):
    """The Unipile invite body must OMIT the message key for a bare invite :
    LinkedIn rejects an empty-string note, absent key = connect without note."""
    from dataclasses import replace
    from backend.providers.unipile import UnipileProvider
    from backend.providers.base import LeadPayload
    prov = UnipileProvider(dry_run=True)
    lead = LeadPayload(
        event_id=1, prospect_id=1, identity="x", first_name="M", last_name="R",
        full_name="Maya R", linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
        company=None, position=None, note="", message="", works_on=None,
        offers=None, seeks=None, fit_score=None, fit_reason=None, sources=None)
    assert "message" not in prov._build_invite_payload(lead, "prov_1")
    assert prov._build_invite_payload(replace(lead, note="hi there"),
                                      "prov_1")["message"] == "hi there"


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
        send_capture(p.id, _req(), SendIn(), db, user)
    assert ei.value.status_code == 404


# ── shared helper directly : warm vs cold routing ──────────────────────────

def _cold_event_and_prospect(db, user):
    ev = models.Event(
        user_id=user.id, kind="planned", role="Eng", seniority="Staff+",
        co_stage="Seed", headcount=40, format="Sit-down dinner",
        city="New York", goal="Hiring pipeline", budget=8000,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="maya-c", name="Maya Rodriguez",
        role="Staff Engineer", company="Acme",
        linkedin_url="https://www.linkedin.com/in/maya-rodriguez")
    db.add(p); db.flush()
    return ev, p


def test_inperson_copy_differs_from_cold_copy(db, user, monkeypatch):
    """compose() must write 'we just met' copy for in_person events, distinct
    from the cold-invite copy for a planned event."""
    monkeypatch.setenv("OUTREACH_COMPOSE_DISABLE", "1")   # deterministic template
    from backend.agents.outreach import compose

    ev = _make_event(db, user)                            # in_person, label set
    ip_event = db.get(models.Event, ev["event_id"])
    p_ip = models.Prospect(
        event_id=ip_event.id, identity="maya-ip", name="Maya Rodriguez",
        role="Staff Engineer", company="Acme",
        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
        note="your work on vector DBs")
    db.add(p_ip); db.flush()

    _cold_ev, p_cold = _cold_event_and_prospect(db, user)

    ip = compose(p_ip, ip_event)
    cold = compose(p_cold, _cold_ev)

    # Distinct copy on BOTH halves.
    assert ip.note != cold.note
    assert ip.message != cold.message
    # In-person references the in-person meeting + the event label.
    assert "NYC Tech Week" in ip.note
    assert "meeting" in ip.note.lower() or "meet you" in ip.message.lower()
    # prospect.note is woven into the in-person copy.
    assert "vector DBs" in ip.note
    # Still fits LinkedIn's connect-request cap.
    assert len(ip.note) <= 300
    # Cold copy is the invite framing, not a post-meeting note.
    assert "vector DBs" not in cold.note


def test_webhook_auto_dm_uses_inperson_copy(db, user, monkeypatch):
    """The webhook auto-DM path (_trigger_auto_dm -> compose) must produce the
    warm in-person DM for an in_person prospect, not a cold re-pitch."""
    monkeypatch.setenv("OUTREACH_COMPOSE_DISABLE", "1")
    from backend.providers.unipile import UnipileProvider
    from backend.routes.webhooks import _trigger_auto_dm

    ip_event = db.get(models.Event, _make_event(db, user)["event_id"])
    p = models.Prospect(
        event_id=ip_event.id, identity="maya-w", name="Maya Rodriguez",
        role="Staff Engineer", company="Acme",
        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
        linkedin_provider_id="dry_li_maya", note="the latency demo")
    db.add(p); db.commit()

    provider = UnipileProvider(dry_run=True, account_id="operator_acct")
    _trigger_auto_dm(db, provider, p)

    sent = [o for o in p.outreach if o.state == "message_sent"]
    assert sent, "auto-DM should have logged a message_sent row"
    body = sent[-1].body
    # Warm in-person DM, referencing the meeting + the conversation note.
    assert "meet you at NYC Tech Week" in body
    assert "the latency demo" in body


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


# ── #2 : resolve(text) returns a RANKED candidate list (mock Exa) ────────────

def _fake_exa_client(results):
    """A MagicMock standing in for httpx.Client returning these Exa results."""
    from unittest.mock import MagicMock
    resp = MagicMock(); resp.status_code = 200
    resp.json.return_value = {"results": results}
    client = MagicMock()
    client.post.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    return client


def test_resolve_text_returns_ranked_candidates(db, user, monkeypatch):
    from unittest.mock import patch
    from backend.routes.inperson import resolve_identity, ResolveIn
    monkeypatch.setenv("EXA_API_KEY", "test-key")   # exa_available() -> True
    results = [
        {"url": "https://www.linkedin.com/in/maya-rodriguez",
         "title": "Maya Rodriguez - Staff Engineer at Acme | LinkedIn",
         "text": "# Maya Rodriguez\nStaff Engineer | ML infra\nSan Francisco"},
        {"url": "https://www.linkedin.com/in/maya-r",
         "title": "Maya R - Engineer at Beta | LinkedIn",
         "text": "# Maya R\nEngineer\nNYC"},
    ]
    with patch("httpx.Client", return_value=_fake_exa_client(results)):
        out = resolve_identity(
            ResolveIn(method="text", name="Maya Rodriguez",
                      title="Staff Engineer", company="Acme"), db, user)
    assert out["method"] == "text"
    assert out["count"] == 2
    # Ranked in Exa's order; each is a low-confidence candidate with a URL.
    assert out["candidates"][0]["name"] == "Maya Rodriguez"
    assert out["candidates"][0]["linkedin_url"] == "https://www.linkedin.com/in/maya-rodriguez"
    assert out["candidates"][0]["confidence"] == "low"
    assert all(c.get("linkedin_url") for c in out["candidates"])


# ── #3 : free text cannot create a Prospect / send without /scan confirm ─────

def test_text_resolve_creates_nothing_only_scan_confirms(db, user, monkeypatch):
    from unittest.mock import patch
    from backend.routes.inperson import resolve_identity, ResolveIn, scan_capture, ScanIn
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    results = [{"url": "https://www.linkedin.com/in/maya-rodriguez",
                "title": "Maya Rodriguez - Staff Engineer at Acme | LinkedIn",
                "text": "# Maya Rodriguez\nStaff Engineer"}]
    ev = _make_event(db, user)
    with patch("httpx.Client", return_value=_fake_exa_client(results)):
        out = resolve_identity(
            ResolveIn(method="text", name="Maya Rodriguez"), db, user)
    assert out["count"] == 1
    # Resolving text must NOT create a Prospect or any OutreachLog.
    assert db.query(models.Prospect).count() == 0
    assert db.query(models.OutreachLog).count() == 0

    # The ONLY way text becomes a Prospect: the user confirms a candidate,
    # whose URL is then sent through /scan.
    chosen = out["candidates"][0]
    scan_capture(ScanIn(event_id=ev["event_id"], linkedin_url=chosen["linkedin_url"],
                        source="text", name=chosen["name"]), db, user)
    assert db.query(models.Prospect).count() == 1
    p = db.query(models.Prospect).one()
    assert p.status == "pending" and p.source == "text"
    # /scan still does not send anything.
    assert db.query(models.OutreachLog).count() == 0


# ── #5 : send warm DM (via is_relation) through the /send route + log ────────

def test_send_capture_warm_dm_via_route(db, user, monkeypatch):
    from backend.providers.unipile import UnipileProvider
    from backend.routes.inperson import scan_capture, ScanIn, send_capture, SendIn
    monkeypatch.setattr(UnipileProvider, "is_relation", lambda self, url: True)
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    p = db.query(models.Prospect).one()

    out = send_capture(p.id, _req(), SendIn(), db, user)
    assert out["path_taken"] == "warm"           # DM, not invite
    assert out["dry_run"] is True
    assert out["state"] == "dry_run_queued"
    # OutreachLog row written for the send.
    assert any(o.channel == "linkedin" for o in p.outreach)


# ── #6 : new_relation matches the scan-created Prospect -> auto-DM ───────────

def _seed_scanned(db, user, handle="maya-rodriguez", name="Maya Rodriguez"):
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url=f"https://www.linkedin.com/in/{handle}",
                        source="scan", name=name, note="the latency demo"), db, user)
    return ev, db.query(models.Prospect).one()


def test_new_relation_matches_scan_prospect_fires_inperson_auto_dm(db, user, monkeypatch):
    monkeypatch.setenv("OUTREACH_COMPOSE_DISABLE", "1")   # deterministic template
    from backend.providers.unipile import UnipileProvider
    from backend.routes.webhooks import _apply_canonical_event, _trigger_auto_dm

    _ev, p = _seed_scanned(db, user)
    assert p.linkedin_provider_id == "dry_li_maya-rodriguez"

    provider = UnipileProvider(dry_run=True, account_id="operator_acct")
    # Simulated Unipile webhook : they accepted the invite.
    canonical = provider.normalize_webhook(
        {"event": "new_relation", "user_provider_id": "dry_li_maya-rodriguez"})
    assert canonical.state == "invite_accepted"

    applied, _reason, prospect = _apply_canonical_event(db, provider, canonical)
    assert applied is True and prospect.id == p.id
    assert prospect.connection_status == "connected"

    _trigger_auto_dm(db, provider, prospect)
    sent = [o for o in prospect.outreach if o.state == "message_sent"]
    assert sent, "in-person auto-DM should have fired on accept"
    # Warm in-person copy, not a cold re-pitch.
    assert "meet you at NYC Tech Week" in sent[-1].body


def test_unmatched_new_relation_fires_nothing(db, user):
    from backend.providers.unipile import UnipileProvider
    from backend.routes.webhooks import _apply_canonical_event

    _ev, p = _seed_scanned(db, user)
    before = db.query(models.OutreachLog).count()

    provider = UnipileProvider(dry_run=True, account_id="operator_acct")
    canonical = provider.normalize_webhook(
        {"event": "new_relation", "user_provider_id": "dry_li_someone_else"})
    applied, reason, prospect = _apply_canonical_event(db, provider, canonical)

    assert applied is False and prospect is None      # no match
    # Nothing written, nothing sent : the route would skip the auto-DM.
    assert db.query(models.OutreachLog).count() == before
    db.refresh(p)
    assert p.status == "pending"                       # untouched


# ── #7 : captures list reflects status, last touch, reply state ──────────────

def test_captures_reflects_last_touch_and_reply(db, user):
    from backend.providers.unipile import UnipileProvider
    from backend.providers.base import CanonicalEvent
    from backend.routes.webhooks import _apply_canonical_event
    from backend.routes.inperson import list_captures
    from datetime import datetime, timezone, timedelta

    ev, p = _seed_scanned(db, user)
    provider = UnipileProvider(dry_run=True, account_id="operator_acct")
    pid = "dry_li_maya-rodriguez"
    t0 = datetime.now(timezone.utc)

    # DM sent, then they replied : feed both canonical events through the
    # same path the webhook uses.
    for i, state in enumerate(("message_sent", "message_replied")):
        _apply_canonical_event(db, provider, CanonicalEvent(
            event_id=0, prospect_id=0, state=state, provider="unipile",
            provider_lead_id=pid, ts=t0 + timedelta(minutes=i),
            body="hi" if state == "message_replied" else "", raw={}))

    out = list_captures(ev["event_id"], db, user)
    row = out["captures"][0]
    assert row["status"] == "rsvp"                       # reply flips to rsvp
    assert row["last_outreach"]["state"] == "message_replied"   # latest touch
    assert row["connection_status"] == "unknown"          # never relation-checked


# ── free connect + host-scoped send gate ─────────────────────────────────────

@pytest.fixture
def connected_unpaid_user(db):
    """LinkedIn connected, NOT paid : can send on the in-person host, blocked
    on the apex."""
    u = models.User(name="Unpaid", email="unpaid@example.com",
                    unipile_account_id="unpaid_acct", linkedin_status="active",
                    paid_at=None)
    db.add(u); db.commit()
    return u


def _scan_one(db, user):
    from backend.routes.inperson import scan_capture, ScanIn
    ev = _make_event(db, user)
    scan_capture(ScanIn(event_id=ev["event_id"],
                        linkedin_url="https://www.linkedin.com/in/maya-rodriguez",
                        source="scan", name="Maya Rodriguez"), db, user)
    return db.query(models.Prospect).one()


def test_send_free_on_inperson_host_for_connected_unpaid(db, connected_unpaid_user):
    """Connected-but-unpaid user CAN send from event.surpluslayer.com."""
    from backend.routes.inperson import send_capture, SendIn
    p = _scan_one(db, connected_unpaid_user)
    out = send_capture(p.id, _req("event.surpluslayer.com"), SendIn(),
                       db, connected_unpaid_user)
    assert out["state"] == "dry_run_queued"   # dry-run send went through


def test_send_paywalled_on_apex_for_connected_unpaid(db, connected_unpaid_user):
    """Same user is still paywalled (402) when the request is from the apex."""
    from fastapi import HTTPException
    from backend.routes.inperson import send_capture, SendIn
    p = _scan_one(db, connected_unpaid_user)
    with pytest.raises(HTTPException) as ei:
        send_capture(p.id, _req("www.surpluslayer.com"), SendIn(),
                     db, connected_unpaid_user)
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "payment_required"


def test_send_inperson_host_still_requires_linkedin_connection(db, user, monkeypatch):
    """Even on the in-person host, a user with NO connected LinkedIn is asked
    to connect (402 linkedin_send_locked) : connection is mechanically needed."""
    from fastapi import HTTPException
    from backend.routes.inperson import send_capture, SendIn
    # Drop the user's LinkedIn connection.
    user.unipile_account_id = None
    user.linkedin_status = "disconnected"
    db.commit()
    p = _scan_one(db, user)  # _make_event still works; scan uses preview provider
    with pytest.raises(HTTPException) as ei:
        send_capture(p.id, _req("event.surpluslayer.com"), SendIn(), db, user)
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "linkedin_send_locked"


def test_linkedin_start_no_longer_requires_payment(db, monkeypatch):
    """POST /linkedin/start used to 402 anonymous/unpaid callers (pay-first).
    Connect is now free : the gate is removed. We stub the Unipile POST so no
    network call happens and assert no 402 is raised before it."""
    import asyncio
    from starlette.requests import Request
    import backend.routes.auth as auth_routes

    monkeypatch.setenv("UNIPILE_DSN", "https://api.unipile.test")
    monkeypatch.setenv("UNIPILE_API_KEY", "k")

    async def _fake_post(dsn, api_key, body):
        return 200, {"url": "https://unipile.test/hosted/abc"}
    monkeypatch.setattr(auth_routes, "_post_hosted_link", _fake_post)

    req = Request({"type": "http", "method": "POST",
                   "path": "/api/auth/linkedin/start",
                   "headers": [(b"origin", b"https://event.surpluslayer.com")],
                   "query_string": b""})
    # Anonymous caller (no session cookie) : previously 402, now succeeds.
    resp = asyncio.run(auth_routes.linkedin_start(req, db))
    import json
    payload = json.loads(bytes(resp.body))
    assert payload["url"] == "https://unipile.test/hosted/abc"
