"""
Tests for the follow-up DM machinery : compose_followup() copy contract
and the eligibility query in routes/admin.py.

Does NOT import backend.main (which transitively pulls schemas.py and its
`str | None` annotations that don't parse on Python 3.9). Instead exercises
the eligibility function + send loop directly with an in-memory SQLAlchemy
session, the same pattern test_scorer.py / test_matcher.py use.

No network : UnipileProvider is forced into dry-run.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config, models
from backend.agents.outreach import compose_followup
from backend.db import Base
from backend.providers import reset_provider_cache
from backend.routes.admin import _eligible_prospects, run_followups


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
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


def _seed(db, *, last_message_hours_ago: float, replied: bool = False,
          followup_count: int = 0, status: str = "contacted"):
    ev = models.Event(
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
        body="hello", ts=now - timedelta(hours=last_message_hours_ago),
        provider="unipile", provider_lead_id="chat_1",
    ))
    if replied:
        db.add(models.OutreachLog(
            prospect_id=p.id, channel="linkedin", state="message_replied",
            body="yes!",
            ts=now - timedelta(hours=max(last_message_hours_ago - 1, 0)),
            provider="unipile",
        ))
    for i in range(followup_count):
        db.add(models.OutreachLog(
            prospect_id=p.id, channel="linkedin", state="follow_up_sent",
            body="nudge", ts=now - timedelta(hours=1),
            provider="unipile", provider_lead_id=f"fu_{i}",
        ))
    db.commit()
    return ev, p


# ── compose_followup ────────────────────────────────────────────────────

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
    # off-ramp must be present so the recipient has an easy out
    assert "not the right fit" in text


def test_compose_followup_handles_csv_multi_select():
    """Multi-select stores goal/seniority/co_stage as CSV : _framing should
    pick the first entry rather than crash on KeyError."""
    event = SimpleNamespace(
        role="ML platform engineers", seniority="Staff+,Senior",
        co_stage="Seed,Series A", headcount=40, format="Sit-down dinner",
        city="San Francisco", goal="Hiring pipeline,Sales pipeline",
        budget=8000,
    )
    prospect = SimpleNamespace(name="Maya", works_on="observability")
    text = compose_followup(prospect, event)
    assert "hiring" in text.lower()


# ── eligibility query ──────────────────────────────────────────────────

def test_eligible_when_past_delay(db):
    _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 1)
    rows = _eligible_prospects(db)
    assert len(rows) == 1


def test_not_eligible_when_too_recent(db):
    _seed(db, last_message_hours_ago=1)
    assert _eligible_prospects(db) == []


def test_not_eligible_after_reply(db):
    _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 10,
          replied=True)
    assert _eligible_prospects(db) == []


def test_not_eligible_at_max_followups(db):
    _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 10,
          followup_count=config.FOLLOWUP_MAX_PER_PROSPECT)
    assert _eligible_prospects(db) == []


def test_anchor_floats_to_most_recent_outbound_touch(db, monkeypatch):
    """Timing anchors on the LAST thing we sent, not the first DM. An old DM
    plus a recent follow-up should NOT re-trigger : the recent follow-up is
    the live anchor and it's still inside the delay window. We raise the cap
    so the eligibility decision is driven by timing, not the per-prospect
    follow-up cap."""
    monkeypatch.setattr(config, "FOLLOWUP_MAX_PER_PROSPECT", 5)
    ev, p = _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 100)
    # A follow-up went out just an hour ago : that's the most recent touch.
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="follow_up_sent",
        body="nudge", ts=datetime.now(timezone.utc) - timedelta(hours=1),
        provider="unipile", provider_lead_id="fu_recent",
    ))
    db.commit()
    # Anchored on the recent follow-up, not the stale first DM : too soon.
    assert _eligible_prospects(db) == []


def test_eligible_when_last_followup_aged_out(db, monkeypatch):
    """Once the most recent outbound touch (a prior follow-up) ages past the
    delay window, the prospect is due for the next nudge again."""
    monkeypatch.setattr(config, "FOLLOWUP_MAX_PER_PROSPECT", 5)
    ev, p = _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 100)
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="follow_up_sent",
        body="nudge",
        ts=datetime.now(timezone.utc) - timedelta(
            hours=config.FOLLOWUP_DELAY_HOURS + 1),
        provider="unipile", provider_lead_id="fu_old",
    ))
    db.commit()
    rows = _eligible_prospects(db)
    assert len(rows) == 1


def test_not_eligible_without_message_sent(db):
    """A prospect that only has an invite_sent row shouldn't be touched :
    they haven't accepted yet, so there's no DM to follow up on."""
    ev = models.Event(
        role="x", seniority="Senior", co_stage="Seed", headcount=40,
        format="Sit-down dinner", city="SF", goal="Hiring pipeline",
        budget=8000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="x", name="X", role="x", company="x",
        status="contacted",
    )
    db.add(p); db.flush()
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="invite_sent",
        body="", ts=datetime.now(timezone.utc) - timedelta(days=5),
    ))
    db.commit()
    assert _eligible_prospects(db) == []


# ── send loop ──────────────────────────────────────────────────────────

def test_run_followups_writes_log_row_and_reports_sent(db):
    _ev, p = _seed(db, last_message_hours_ago=config.FOLLOWUP_DELAY_HOURS + 5)
    result = run_followups(db=db, _=None)
    assert result["eligible"] == 1
    assert result["sent"] == 1
    assert result["failed"] == 0
    db.expire_all()
    refreshed = db.get(models.Prospect, p.id)
    states = [o.state for o in refreshed.outreach]
    assert states.count("follow_up_sent") == 1


def test_run_followups_noop_when_nothing_eligible(db):
    _seed(db, last_message_hours_ago=1)
    result = run_followups(db=db, _=None)
    assert result == {
        "eligible": 0, "sent": 0, "failed": 0,
        "delay_hours": config.FOLLOWUP_DELAY_HOURS,
        "max_per_prospect": config.FOLLOWUP_MAX_PER_PROSPECT,
        "results": [], "errors": [],
    }
