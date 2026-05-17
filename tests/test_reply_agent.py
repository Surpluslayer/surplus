"""
Tests for the AI reply agent harness — decide_reply() output validation,
should_auto_send() gate, and the admin pending-reply endpoints.

No network. The Anthropic client is mocked everywhere — we never hit Claude
from tests. UnipileProvider is forced into dry-run.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.reply_agent import (
    AUTO_SEND_CLASSES,
    ReplyDecision,
    ThreadMessage,
    decide_reply,
    should_auto_send,
)
from backend.db import Base
from backend.providers import reset_provider_cache
from backend.routes.admin import (
    approve_pending_reply, list_pending_replies, reject_pending_reply,
    ApproveBody, RejectBody,
)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
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


def _fake_anthropic_response(json_body: str) -> MagicMock:
    """Mock the AsyncAnthropic / Anthropic response shape we read."""
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    # Strip leading "{" since decide_reply prefills with it
    block.text = json_body.lstrip("{").rstrip()
    resp.content = [block]
    resp.stop_reason = "end_turn"
    return resp


def _fake_event(**overrides):
    base = dict(
        role="ML platform engineers",
        seniority="Staff+",
        co_stage="Seed",
        headcount=40,
        format="Sit-down dinner",
        city="San Francisco",
        goal="Hiring pipeline",
        budget=8000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_prospect():
    return SimpleNamespace(
        name="Maya Rodriguez",
        role="Staff Infra Engineer",
        company="Lo91r",
        works_on="observability",
    )


# ── decide_reply() ──────────────────────────────────────────────────────

def test_decide_reply_parses_clarifying_classification():
    client = MagicMock()
    client.messages.create.return_value = _fake_anthropic_response(
        '{"classification":"clarifying","draft_text":"7pm at our usual spot — '
        'I\'ll send the exact address once you confirm.","reasoning":"asked about time"}'
    )
    decision = decide_reply(
        [ThreadMessage(direction="inbound", text="What time?")],
        _fake_event(), _fake_prospect(), host=None, client=client,
    )
    assert decision.classification == "clarifying"
    assert "7pm" in decision.draft_text
    assert decision.error is None


def test_decide_reply_coerces_unknown_classification_to_ambiguous():
    """Defensive: the model might return a class we don't know about.
    Should fall through to 'ambiguous' rather than crash."""
    client = MagicMock()
    client.messages.create.return_value = _fake_anthropic_response(
        '{"classification":"yolo","draft_text":"hi","reasoning":"r"}'
    )
    decision = decide_reply(
        [], _fake_event(), _fake_prospect(), host=None, client=client,
    )
    assert decision.classification == "ambiguous"


def test_decide_reply_returns_ambiguous_on_unparseable_response():
    client = MagicMock()
    client.messages.create.return_value = _fake_anthropic_response("not json at all")
    decision = decide_reply(
        [], _fake_event(), _fake_prospect(), host=None, client=client,
    )
    assert decision.classification == "ambiguous"
    assert decision.error  # error string set


def test_decide_reply_catches_anthropic_exception():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("api down")
    decision = decide_reply(
        [], _fake_event(), _fake_prospect(), host=None, client=client,
    )
    assert decision.classification == "ambiguous"
    assert "api down" in decision.error


# ── should_auto_send() gate ─────────────────────────────────────────────

def test_should_auto_send_true_for_clarifying_first_time():
    d = ReplyDecision(classification="clarifying", draft_text="hi",
                      reasoning="r")
    assert should_auto_send(d, prior_auto_send_count=0) is True


def test_should_auto_send_false_after_loop_guard():
    d = ReplyDecision(classification="clarifying", draft_text="hi",
                      reasoning="r")
    assert should_auto_send(d, prior_auto_send_count=1) is False


@pytest.mark.parametrize("klass", ["commitment", "off_topic", "negative",
                                   "ambiguous"])
def test_should_auto_send_false_for_non_clarifying(klass):
    d = ReplyDecision(classification=klass, draft_text="x", reasoning="r")
    assert should_auto_send(d, prior_auto_send_count=0) is False


def test_should_auto_send_false_for_empty_draft():
    d = ReplyDecision(classification="clarifying", draft_text="   ",
                      reasoning="r")
    assert should_auto_send(d, prior_auto_send_count=0) is False


def test_should_auto_send_false_when_decision_has_error():
    d = ReplyDecision(classification="clarifying", draft_text="hi",
                      reasoning="r", error="oops")
    assert should_auto_send(d, prior_auto_send_count=0) is False


def test_auto_send_classes_is_intentionally_narrow():
    """Spec contract: only 'clarifying' auto-sends in v1. If this set grows,
    the trust review goes with it — fail loudly to force the conversation."""
    assert AUTO_SEND_CLASSES == frozenset({"clarifying"})


# ── pending-reply admin endpoints ──────────────────────────────────────

def _seed_pending(db, classification="commitment", status="pending"):
    ev = models.Event(
        role="x", seniority="Senior", co_stage="Seed", headcount=40,
        format="Sit-down dinner", city="SF", goal="Hiring pipeline",
        budget=8000, threshold=70,
    )
    db.add(ev); db.flush()
    p = models.Prospect(
        event_id=ev.id, identity="m", name="Maya", role="Eng", company="X",
        seniority="Staff+", side="Builds", works_on="observability",
        offers="depth", seeks="role", li_resolved=True,
        linkedin_url="https://www.linkedin.com/in/maya",
        linkedin_provider_id="li_maya", sources="linkedin", fit_score=88,
        status="contacted",
    )
    db.add(p); db.flush()
    pr = models.PendingReply(
        prospect_id=p.id, inbound_body="Can you confirm 8pm?",
        classification=classification,
        draft_text="Let me check with the host and circle back today.",
        reasoning="implies commitment to a time",
        status=status,
    )
    db.add(pr); db.commit()
    return ev, p, pr


def test_list_pending_replies_returns_only_pending(db):
    _seed_pending(db, status="pending")
    _seed_pending(db, status="approved")
    out = list_pending_replies(db=db, _=None)
    assert len(out) == 1
    assert out[0].status == "pending"


def test_approve_sends_and_marks(db):
    _ev, _p, pr = _seed_pending(db)
    res = approve_pending_reply(
        pending_id=pr.id, body=ApproveBody(), db=db, _=None,
    )
    assert res["sent"] is True
    db.expire_all()
    refreshed = db.get(models.PendingReply, pr.id)
    assert refreshed.status == "approved"
    assert refreshed.final_text == pr.draft_text


def test_approve_with_edited_text_sends_the_edit(db):
    _ev, _p, pr = _seed_pending(db)
    res = approve_pending_reply(
        pending_id=pr.id, body=ApproveBody(edited_text="edited reply"),
        db=db, _=None,
    )
    assert res["sent"] is True
    db.expire_all()
    refreshed = db.get(models.PendingReply, pr.id)
    assert refreshed.final_text == "edited reply"


def test_approve_404_when_already_decided(db):
    _ev, _p, pr = _seed_pending(db, status="approved")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        approve_pending_reply(
            pending_id=pr.id, body=ApproveBody(), db=db, _=None,
        )
    assert exc.value.status_code == 404


def test_reject_marks_and_does_not_send(db):
    _ev, _p, pr = _seed_pending(db)
    res = reject_pending_reply(
        pending_id=pr.id, body=RejectBody(reason="off-tone"), db=db, _=None,
    )
    assert res["status"] == "rejected"
    db.expire_all()
    refreshed = db.get(models.PendingReply, pr.id)
    assert refreshed.status == "rejected"
    assert refreshed.final_text is None
    # No outreach log row created
    assert refreshed.prospect.outreach == []
