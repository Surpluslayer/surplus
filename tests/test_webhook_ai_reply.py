"""
Tests for the webhook → reply-agent bridge.

These pin the load-bearing wiring between Unipile's `message_replied`
event and the AI agent:

  - Auto-send fires when (and only when) classification is in the
    allow-list AND the loop guard hasn't tripped.
  - Otherwise a PendingReply row is created with the correct fields.
  - Inbound body is capped at the configured size.
  - Thread is built from fetch_thread + the canonical event body.
  - Missing event short-circuits without writing rows.

Patches `decide_reply` so the model is never actually called; we're
testing the dispatch logic, not the model.
"""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.relationship.reply_agent import ReplyDecision
from backend.db import Base
from backend.providers import get_provider, reset_provider_cache
from backend.providers.base import CanonicalEvent
from backend.routes.webhooks import _handle_ai_reply, _INBOUND_BODY_MAX


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
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


def _seed_prospect(db, *, prior_auto_replies: int = 0):
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
        offers="Observability depth", seeks="role",
        li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya",
        linkedin_provider_id="li_maya",
        sources="linkedin", fit_score=88, status="contacted",
    )
    db.add(p); db.flush()
    # Required so _last_chat_id finds the chat
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="message_sent",
        body="first DM", ts=datetime.now(timezone.utc),
        provider="unipile", provider_lead_id="chat_xyz",
    ))
    for _ in range(prior_auto_replies):
        db.add(models.OutreachLog(
            prospect_id=p.id, channel="linkedin", state="auto_reply_sent",
            body="prior auto", ts=datetime.now(timezone.utc),
            provider="unipile", provider_lead_id="prev_auto",
        ))
    db.commit()
    return ev, p


def _canonical(body: str = "what time?") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=0, prospect_id=0,
        state="message_replied", provider="unipile",
        provider_lead_id="li_maya",
        ts=datetime.now(timezone.utc), body=body, raw={},
    )


def _decision(classification: str, draft: str = "Dinner is at 7pm."):
    return ReplyDecision(
        classification=classification, draft_text=draft,
        reasoning=f"classified as {classification}",
    )


# ── Auto-send path ──────────────────────────────────────────────────────

def test_clarifying_class_queues_while_kill_switch_active(db):
    # KILL SWITCH: AUTO_SEND_CLASSES is empty, so even a clarifying message
    # queues for human approval instead of auto-sending. Nothing leaves the
    # host's LinkedIn. When the kill switch is lifted, restore the auto_sent
    # assertion below.
    _ev, p = _seed_prospect(db)
    with patch("backend.routes.webhooks.decide_reply",
               return_value=_decision("clarifying")):
        result = _handle_ai_reply(db, get_provider(), p, _canonical())
    assert result["action"] == "queued"
    assert result["classification"] == "clarifying"
    db.expire_all()
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert "auto_reply_sent" not in states
    assert db.query(models.PendingReply).count() == 1


def test_loop_guard_queues_after_first_auto_reply(db):
    """Even a 'clarifying' second message must queue once we've already
    auto-replied once in this conversation."""
    _ev, p = _seed_prospect(db, prior_auto_replies=1)
    with patch("backend.routes.webhooks.decide_reply",
               return_value=_decision("clarifying", draft="Yes, dinner is at 7.")):
        result = _handle_ai_reply(db, get_provider(), p, _canonical())
    assert result["action"] == "queued"
    pending = db.query(models.PendingReply).all()
    assert len(pending) == 1
    assert pending[0].classification == "clarifying"


@pytest.mark.parametrize("klass", ["commitment", "off_topic", "negative", "ambiguous"])
def test_non_clarifying_class_queues(klass, db):
    _ev, p = _seed_prospect(db)
    with patch("backend.routes.webhooks.decide_reply",
               return_value=_decision(klass)):
        result = _handle_ai_reply(db, get_provider(), p, _canonical("can you confirm?"))
    assert result["action"] == "queued"
    assert result["classification"] == klass
    pending = db.query(models.PendingReply).one()
    assert pending.classification == klass
    assert pending.status == "pending"
    assert pending.inbound_body == "can you confirm?"


def test_inbound_body_is_capped_at_max(db):
    """A malicious huge inbound webhook body shouldn't bloat the DB."""
    _ev, p = _seed_prospect(db)
    huge = "x" * (_INBOUND_BODY_MAX + 10_000)
    with patch("backend.routes.webhooks.decide_reply",
               return_value=_decision("commitment")):
        _handle_ai_reply(db, get_provider(), p, _canonical(huge))
    pending = db.query(models.PendingReply).one()
    assert len(pending.inbound_body) == _INBOUND_BODY_MAX


def test_short_circuits_when_event_is_missing(db):
    """A prospect with no event should not blow up the webhook : return None
    silently rather than crashing the whole handler."""
    p = models.Prospect(
        event_id=999999, identity="ghost", name="Ghost", role="?", company="?",
        status="contacted",
    )
    db.add(p); db.commit()
    # Force the lazy-loaded relationship to None by manipulating directly
    p.event = None
    with patch("backend.routes.webhooks.decide_reply") as decide:
        result = _handle_ai_reply(db, get_provider(), p, _canonical())
    assert result is None
    decide.assert_not_called()
    assert db.query(models.PendingReply).count() == 0


def test_thread_always_includes_canonical_body(db):
    """fetch_thread may not include the just-arrived message (Unipile is
    eventually consistent). _handle_ai_reply must append it manually so the
    model always sees what it's responding to."""
    _ev, p = _seed_prospect(db)
    seen_threads: list = []

    def capture(thread, *args, **kwargs):
        seen_threads.append(thread)
        return _decision("commitment")

    with patch("backend.routes.webhooks.decide_reply", side_effect=capture):
        _handle_ai_reply(db, get_provider(), p, _canonical("are you there?"))

    assert seen_threads, "decide_reply was not called"
    texts = [m.text for m in seen_threads[0]]
    assert "are you there?" in texts
