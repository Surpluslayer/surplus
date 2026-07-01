"""
Tests for the scheduled follow-up machinery (the "Gmail Schedule Send" model):

  - compose_followup() copy contract (the template fallback draft)
  - followup_scheduler: stage_followup() idempotency, suggest_send_time(),
    cancel_pending_followups()
  - routes/admin.run_followups: dispatches due rows, skips future ones,
    defensively cancels if the recipient replied
  - routes/followups: list / patch / cancel / send-now, owner-scoped

Does NOT import backend.main (which transitively pulls schemas.py and its
`str | None` annotations that don't parse on Python 3.9). Exercises the
functions directly against an in-memory SQLAlchemy session, the same pattern
test_scorer.py / test_matcher.py use.

No network : UnipileProvider is forced into dry-run and the follow-up composer
is forced onto its deterministic template (FOLLOWUP_COMPOSE_DISABLE).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.relationship.followup_scheduler import (
    _last_sent_message,
    _user_message,
    cancel_pending_followups,
    pending_followup,
    stage_followup,
    suggest_send_time,
)
from backend.agents.outreach import compose_followup
from backend.db import Base
from backend.providers import reset_provider_cache
from backend.routes import followups as followups_route
from backend.routes.admin import _due_followups, run_followups


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    # Force the deterministic template : no Anthropic call in tests.
    monkeypatch.setenv("FOLLOWUP_COMPOSE_DISABLE", "1")
    reset_provider_cache()

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        reset_provider_cache()


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes on readback; coerce to UTC for compares."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _seed(db, *, replied: bool = False, status: str = "contacted"):
    """A user + event + prospect with a first DM already sent.

    The legacy per-user auto_followups_enabled column is left at its default
    (False): neither staging nor dispatch reads it anymore."""
    user = models.User(email="host@example.com")
    db.add(user); db.flush()
    ev = models.Event(
        user_id=user.id,
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="maya", name="Maya Rodriguez",
        role="Staff Infra Engineer", company="Lo91r", seniority="Staff+",
        side="Builds", works_on="observability",
        offers="Observability depth", seeks="Staff-scope role",
        li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya",
        linkedin_provider_id="li_maya_123",
        sources="linkedin", fit_score=88, status=status,
    )
    db.add(p); db.flush()

    now = datetime.now(timezone.utc)
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="message_sent",
        body="hello", ts=now - timedelta(hours=1),
        provider="unipile", provider_lead_id="chat_1",
    ))
    if replied:
        db.add(models.OutreachLog(
            prospect_id=p.id, channel="linkedin", state="message_replied",
            body="yes!", ts=now, provider="unipile",
        ))
    db.commit()
    return user, ev, p


def _stage_due(db, p, *, hours_ago: float = 1.0) -> models.ScheduledFollowup:
    """Stage a follow-up and backdate its send_at so it's due now."""
    row = stage_followup(db, p)
    assert row is not None
    row.send_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    db.commit()
    return row


# ── compose_followup (template fallback draft) ───────────────────────────

def test_compose_followup_uses_first_name_and_format():
    event = SimpleNamespace(
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )
    prospect = SimpleNamespace(name="Maya Rodriguez", works_on="observability")
    text = compose_followup(prospect, event)
    assert text.startswith("Hey Maya"), text
    assert "sit-down dinner" in text
    assert "not the right fit" in text


def test_compose_followup_uses_personal_hook():
    event = SimpleNamespace(
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )
    prospect = SimpleNamespace(name="Maya", company="Lo91r",
                               works_on="observability")
    text = compose_followup(prospect, event)
    assert "observability" in text  # grounded in what they work on


def test_compose_followup_opener_acknowledges_prior_message():
    event = SimpleNamespace(
        role="ML platform engineers", seniority="Staff+", co_stage="Seed",
        headcount=40, format="Sit-down dinner", city="San Francisco",
        goal="Hiring pipeline", budget=8000,
    )
    prospect = SimpleNamespace(name="Maya", company="Lo91r",
                               works_on="observability")
    with_prior = compose_followup(prospect, event, prior_message="hello there")
    assert "circling back" in with_prior
    without_prior = compose_followup(prospect, event)
    assert "following up" in without_prior


def test_compose_followup_handles_csv_multi_select():
    event = SimpleNamespace(
        role="ML platform engineers", seniority="Staff+,Senior",
        co_stage="Seed,Series A", headcount=40, format="Sit-down dinner",
        city="San Francisco", goal="Hiring pipeline,Sales pipeline",
        budget=8000,
    )
    prospect = SimpleNamespace(name="Maya", works_on="observability")
    text = compose_followup(prospect, event)
    assert "hiring" in text.lower()


# ── suggest_send_time ────────────────────────────────────────────────────

def test_suggest_send_time_is_future_and_aware():
    t = suggest_send_time()
    assert t.tzinfo is not None
    assert t > datetime.now(timezone.utc)


def test_suggest_send_time_skips_weekend_and_clamps_daytime():
    # A Friday 23:00 base + 12h would land Saturday : must roll to a weekday
    # inside the daytime window.
    friday_night = datetime(2026, 6, 12, 23, 0, tzinfo=timezone.utc)  # Fri
    t = suggest_send_time(after=friday_night)
    assert t.weekday() < 5, f"landed on weekday {t.weekday()}"
    assert 9 <= t.hour < 18


# ── stage_followup ───────────────────────────────────────────────────────

def test_stage_followup_creates_pending_row(db):
    _u, _ev, p = _seed(db)
    row = stage_followup(db, p)
    assert row is not None
    assert row.status == "scheduled"
    assert row.body.strip()
    assert _aware(row.send_at) > datetime.now(timezone.utc)
    assert row.suggested_send_at == row.send_at


def test_last_sent_message_returns_first_dm_body(db):
    _u, _ev, p = _seed(db)
    assert _last_sent_message(db, p.id) == "hello"


def test_user_message_includes_prior_message_section():
    event = SimpleNamespace(format="Sit-down dinner", city="San Francisco",
                            brief="")
    prospect = SimpleNamespace(name="Maya", role="Staff Infra", company="Lo91r",
                               headline=None, works_on="observability")
    msg = _user_message(prospect, event, prior_message="Hey Maya, come to dinner")
    assert "YOUR FIRST MESSAGE" in msg
    assert "Hey Maya, come to dinner" in msg
    assert "do not repeat it" in msg


def test_stage_followup_drafts_even_when_auto_send_off(db):
    """The draft is always created regardless of any send gate: SENDING is
    gated at dispatch by the general-send master (SURPLUS_AUTOMATED_SENDS),
    never at staging. A host with automation off still gets a staged draft
    they can manually send."""
    _u, _ev, p = _seed(db)
    row = stage_followup(db, p)
    assert row is not None
    assert row.status == "scheduled"
    assert row.body.strip()
    assert db.query(models.ScheduledFollowup).filter_by(prospect_id=p.id).count() == 1


def test_stage_followup_is_idempotent(db):
    _u, _ev, p = _seed(db)
    first = stage_followup(db, p)
    second = stage_followup(db, p)
    assert first.id == second.id
    n = db.query(models.ScheduledFollowup).filter_by(prospect_id=p.id).count()
    assert n == 1


# ── cancel ───────────────────────────────────────────────────────────────

def test_cancel_pending_followups_marks_cancelled(db):
    _u, _ev, p = _seed(db)
    stage_followup(db, p)
    n = cancel_pending_followups(db, p.id, reason="replied")
    assert n == 1
    assert pending_followup(db, p.id) is None
    row = db.query(models.ScheduledFollowup).filter_by(prospect_id=p.id).one()
    assert row.status == "cancelled"
    assert row.cancel_reason == "replied"


# ── dispatch (run_followups) ─────────────────────────────────────────────

def test_run_followups_sends_due_row(db, monkeypatch):
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")   # env master opt-in
    _u, _ev, p = _seed(db)
    _u.autonomy_mode = "auto"                               # per-user opt-in
    db.commit()
    row = _stage_due(db, p)
    result = run_followups(db=db, _=None)
    assert result["due"] == 1
    assert result["sent"] == 1
    assert result["failed"] == 0
    db.expire_all()
    refreshed = db.get(models.ScheduledFollowup, row.id)
    assert refreshed.status == "sent"
    assert refreshed.sent_at is not None
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert states.count("follow_up_sent") == 1


def test_run_followups_sends_even_when_user_toggle_off(db, monkeypatch):
    """The legacy per-user auto_followups_enabled column does not gate the
    dispatcher. With the env master on and the user's autonomy_mode 'auto',
    a host with the old toggle off still gets the nudge sent."""
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    _u, _ev, p = _seed(db)
    assert _u.auto_followups_enabled is False  # legacy column, default off
    _u.autonomy_mode = "auto"
    db.commit()
    row = _stage_due(db, p)
    result = run_followups(db=db, _=None)
    assert result["due"] == 1
    assert result["sent"] == 1
    assert result["held"] == 0
    db.expire_all()
    assert db.get(models.ScheduledFollowup, row.id).status == "sent"


def test_run_followups_holds_when_kill_switch_off(db, monkeypatch):
    """Autonomy gate (SURPLUS_AUTOMATED_SENDS, shared with auto-reply) off : a
    due nudge is HELD (left scheduled), never sent or cancelled, so it can
    still be sent manually or dispatched once the user turns autonomy on."""
    monkeypatch.delenv("SURPLUS_AUTOMATED_SENDS", raising=False)
    _u, _ev, p = _seed(db)
    row = _stage_due(db, p)
    result = run_followups(db=db, _=None)
    assert result["due"] == 1
    assert result["sent"] == 0
    assert result["held"] == 1
    assert result["cancelled"] == 0
    db.expire_all()
    refreshed = db.get(models.ScheduledFollowup, row.id)
    assert refreshed.status == "scheduled"
    assert refreshed.sent_at is None


def test_run_followups_expires_stale_row(db, monkeypatch):
    """A row overdue past the staleness window must EXPIRE (cancelled reason
    "stale"), not fire a weeks-late "just checking in" -- guards the backlog
    the moment dispatch opens after an outage."""
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    _u, _ev, p = _seed(db)
    _u.autonomy_mode = "auto"
    db.commit()
    row = _stage_due(db, p, hours_ago=9 * 24)   # 9 days overdue > 7-day window
    result = run_followups(db=db, _=None)
    assert result["due"] == 1
    assert result["sent"] == 0
    assert result["cancelled"] == 1
    db.expire_all()
    refreshed = db.get(models.ScheduledFollowup, row.id)
    assert refreshed.status == "cancelled"
    assert refreshed.cancel_reason == "stale"


def test_run_followups_skips_future_row(db):
    _u, _ev, p = _seed(db)
    stage_followup(db, p)  # send_at in the future by default
    result = run_followups(db=db, _=None)
    assert result["due"] == 0
    assert result["sent"] == 0


def test_run_followups_cancels_due_row_if_replied(db):
    """Defensive: a reply that raced past the webhook cancel must not send."""
    _u, _ev, p = _seed(db, replied=True)
    row = _stage_due(db, p)
    result = run_followups(db=db, _=None)
    assert result["sent"] == 0
    assert result["cancelled"] == 1
    db.expire_all()
    assert db.get(models.ScheduledFollowup, row.id).status == "cancelled"


def test_run_followups_noop_when_queue_empty(db):
    _seed(db)  # no follow-up staged
    result = run_followups(db=db, _=None)
    assert result["due"] == 0
    assert result["sent"] == 0
    assert result["failed"] == 0
    assert result["cancelled"] == 0


# ── user-control routes ──────────────────────────────────────────────────

def test_list_followups_is_owner_scoped(db):
    user, _ev, p = _seed(db)
    stage_followup(db, p)
    other = models.User(email="other@example.com")
    db.add(other); db.commit()

    mine = followups_route.list_followups(db=db, user=user)
    assert len(mine) == 1
    assert mine[0].prospect_name == "Maya Rodriguez"
    theirs = followups_route.list_followups(db=db, user=other)
    assert theirs == []


def test_patch_followup_edits_body_and_time(db):
    user, _ev, p = _seed(db)
    row = stage_followup(db, p)
    new_time = datetime.now(timezone.utc) + timedelta(days=3)
    out = followups_route.update_followup(
        row.id,
        followups_route.FollowupPatch(body="new draft", send_at=new_time),
        db=db, user=user,
    )
    assert out.body == "new draft"
    assert abs((_aware(out.send_at) - new_time).total_seconds()) < 1


def test_patch_rejects_empty_body(db):
    user, _ev, p = _seed(db)
    row = stage_followup(db, p)
    with pytest.raises(HTTPException) as exc:
        followups_route.update_followup(
            row.id, followups_route.FollowupPatch(body="   "),
            db=db, user=user)
    assert exc.value.status_code == 400


def test_cancel_route_marks_cancelled_by_user(db):
    user, _ev, p = _seed(db)
    row = stage_followup(db, p)
    out = followups_route.cancel_followup(row.id, db=db, user=user)
    assert out.status == "cancelled"
    assert out.cancel_reason == "user"


def test_send_now_dispatches_immediately(db):
    user, _ev, p = _seed(db)
    row = stage_followup(db, p)  # future send_at, but send-now ignores it
    out = followups_route.send_followup_now(row.id, db=db, user=user)
    assert out.status == "sent"
    assert out.sent_at is not None
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert "follow_up_sent" in states


def test_routes_404_on_not_owned(db):
    _user, _ev, p = _seed(db)
    row = stage_followup(db, p)
    stranger = models.User(email="stranger@example.com")
    db.add(stranger); db.commit()
    with pytest.raises(HTTPException) as exc:
        followups_route.cancel_followup(row.id, db=db, user=stranger)
    assert exc.value.status_code == 404


# ── send-link wiring (demo / scheduling link) ─────────────────────────────

def test_strip_call_asks_keeps_url_clauses():
    """The no-call hygiene must never eat an attached link: \\bzoom\\b used to
    strip whole clauses containing Zoom meeting URLs from booking drafts."""
    from backend.providers.base import strip_call_asks
    msg = ("Great meeting you! Here is the Zoom link for Tuesday: "
           "https://zoom.us/j/123?pwd=abc. Looking forward to it.")
    assert strip_call_asks(msg) == msg
    # ...while prose call asks are still stripped.
    prose = ("Would love to hop on a quick call next week. "
             "Also, congrats on the launch.")
    out = strip_call_asks(prose)
    assert "call" not in out
    assert "congrats on the launch" in out


def test_followup_body_carries_saved_send_link(db, monkeypatch):
    """Deterministic link guarantee: with the host's reusable saved_send_link
    set, the staged follow-up body always contains it, template path included
    (the LLM weave is best-effort; ensure_send_link is the guarantee)."""
    monkeypatch.setenv("FOLLOWUP_COMPOSE_DISABLE", "1")
    u, _ev, p = _seed(db)
    u.saved_send_link = "https://calendly.com/host/15min"
    db.commit()
    row = stage_followup(db, p)
    assert row is not None
    assert "https://calendly.com/host/15min" in row.body


def test_followup_next_step_url_beats_saved_link(db, monkeypatch):
    """A URL captured in the prospect's next_step wins over the reusable link."""
    monkeypatch.setenv("FOLLOWUP_COMPOSE_DISABLE", "1")
    u, _ev, p = _seed(db)
    u.saved_send_link = "https://calendly.com/host/15min"
    p.next_step = "book a time: https://calendly.com/host/demo-30"
    db.commit()
    row = stage_followup(db, p)
    assert "https://calendly.com/host/demo-30" in row.body
    assert "https://calendly.com/host/15min" not in row.body
