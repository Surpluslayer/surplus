"""
Tests for the per-user AUTONOMY CONTROL (users.autonomy_mode):

  - column default + idempotent startup migration
  - owner_autonomy_mode() normalization
  - GET/PUT /api/settings (values, validation, per-user writes)
  - /api/auth/me exposes autonomy_mode
  - nudge dispatch gate truth table (env master x user mode)
  - AI auto-reply gate honors the mode (webhook _handle_ai_reply)
  - GET /api/followups/pending + send-now / skip owner scoping

Same no-network, direct-call pattern as tests/test_followups.py and
tests/test_webhook_ai_reply.py: an in-memory SQLAlchemy session, the
provider forced into dry-run, and the model patched out.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.agents.relationship.followup_scheduler import stage_followup
from backend.agents.relationship.pipeline.send.sender import owner_autonomy_mode
from backend.agents.relationship.reply_agent import ReplyDecision
from backend.db import Base
from backend.providers import get_provider, reset_provider_cache
from backend.providers.base import CanonicalEvent
from backend.routes import followups as followups_route
from backend.routes import settings as settings_route
from backend.routes.admin import run_followups
from backend.routes.webhooks import _handle_ai_reply


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake_account")
    monkeypatch.setenv("FOLLOWUP_COMPOSE_DISABLE", "1")
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


def _seed(db, *, autonomy: str = "off"):
    """User (+ mode) + event + prospect with a first DM already sent."""
    user = models.User(email="host@example.com", autonomy_mode=autonomy)
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
        sources="linkedin", fit_score=88, status="contacted",
    )
    db.add(p); db.flush()
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state="message_sent",
        body="hello", ts=datetime.now(timezone.utc) - timedelta(hours=1),
        provider="unipile", provider_lead_id="chat_1",
    ))
    db.commit()
    return user, ev, p


def _stage_due(db, p, *, hours_ago: float = 1.0) -> models.ScheduledFollowup:
    row = stage_followup(db, p)
    assert row is not None
    row.send_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    db.commit()
    return row


# ── column default + migration ────────────────────────────────────────────

def test_new_user_defaults_to_off(db):
    u = models.User(email="fresh@example.com")
    db.add(u); db.commit(); db.refresh(u)
    assert u.autonomy_mode == "off"


def test_autonomy_migration_adds_column_and_is_idempotent(monkeypatch):
    """Simulate a pre-migration users table (drop the column), run the
    guarded ALTER, and confirm existing rows read back 'off'. A second run
    must be a clean no-op."""
    from backend import db as dbmod
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "ENGINE", engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(models.User(email="legacy@example.com", name="Legacy"))
        s.commit()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN autonomy_mode"))
    dbmod._migrate_user_autonomy_mode()
    dbmod._migrate_user_autonomy_mode()  # second run: no crash, no dup column
    with engine.connect() as conn:
        val = conn.execute(text(
            "SELECT autonomy_mode FROM users "
            "WHERE email = 'legacy@example.com'")).scalar()
    assert val == "off"


# ── owner_autonomy_mode normalization ─────────────────────────────────────

def test_owner_autonomy_mode_normalizes_bad_values():
    from types import SimpleNamespace
    assert owner_autonomy_mode(None) == "off"
    assert owner_autonomy_mode(SimpleNamespace()) == "off"
    assert owner_autonomy_mode(SimpleNamespace(autonomy_mode=None)) == "off"
    assert owner_autonomy_mode(SimpleNamespace(autonomy_mode="banana")) == "off"
    assert owner_autonomy_mode(SimpleNamespace(autonomy_mode=" AUTO ")) == "auto"
    assert owner_autonomy_mode(SimpleNamespace(autonomy_mode="ask")) == "ask"


# ── settings endpoint ─────────────────────────────────────────────────────

def test_get_settings_returns_normalized_mode(db):
    user, _ev, _p = _seed(db)
    out = settings_route.get_settings(user=user)
    assert out.autonomy_mode == "off"
    user.autonomy_mode = "junk"  # legacy garbage reads back as off
    assert settings_route.get_settings(user=user).autonomy_mode == "off"


@pytest.mark.parametrize("mode", ["off", "ask", "auto"])
def test_put_settings_accepts_canonical_modes(db, mode):
    user, _ev, _p = _seed(db)
    out = settings_route.put_settings(
        settings_route.SettingsPut(autonomy_mode=mode), db=db, user=user)
    assert out.autonomy_mode == mode
    db.refresh(user)
    assert user.autonomy_mode == mode


@pytest.mark.parametrize("bad", ["", "on", "full", "AUTO!", "yes"])
def test_put_settings_rejects_other_values_422(db, bad):
    user, _ev, _p = _seed(db)
    with pytest.raises(HTTPException) as exc:
        settings_route.put_settings(
            settings_route.SettingsPut(autonomy_mode=bad), db=db, user=user)
    assert exc.value.status_code == 422
    db.refresh(user)
    assert user.autonomy_mode == "off"  # unchanged


def test_put_settings_writes_only_the_signed_in_user(db):
    user, _ev, _p = _seed(db)
    other = models.User(email="other@example.com")
    db.add(other); db.commit()
    settings_route.put_settings(
        settings_route.SettingsPut(autonomy_mode="auto"), db=db, user=user)
    db.refresh(other)
    assert other.autonomy_mode == "off"


def test_me_exposes_autonomy_mode(db):
    import json
    from backend.routes import auth as auth_route
    user, _ev, _p = _seed(db, autonomy="ask")
    payload = json.loads(auth_route.me(user).body)
    assert payload["autonomy_mode"] == "ask"


# ── nudge dispatch gate truth table ───────────────────────────────────────
# Unattended nudge fires iff env master ON and owner mode == 'auto'.

@pytest.mark.parametrize("env_on,mode,expect", [
    (False, "off", "held"),
    (False, "ask", "held"),
    (False, "auto", "held"),   # env master is the ops kill switch
    (True, "off", "held"),
    (True, "ask", "held"),     # ask == off at the gate; the UI is the diff
    (True, "auto", "sent"),
])
def test_dispatch_gate_truth_table(db, monkeypatch, env_on, mode, expect):
    if env_on:
        monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    else:
        monkeypatch.delenv("SURPLUS_AUTOMATED_SENDS", raising=False)
    _u, _ev, p = _seed(db, autonomy=mode)
    row = _stage_due(db, p)
    result = run_followups(db=db, _=None)
    assert result["due"] == 1
    if expect == "sent":
        assert result["sent"] == 1 and result["held"] == 0
        db.expire_all()
        assert db.get(models.ScheduledFollowup, row.id).status == "sent"
    else:
        assert result["sent"] == 0 and result["held"] == 1
        db.expire_all()
        refreshed = db.get(models.ScheduledFollowup, row.id)
        assert refreshed.status == "scheduled"   # held, never cancelled
        assert refreshed.sent_at is None


# ── AI auto-reply gate honors the mode ────────────────────────────────────
# should_auto_send is patched TRUE (AUTO_SEND_CLASSES is an empty frozenset
# today, the reply-agent kill switch, so nothing would ever auto-send
# otherwise); these tests pin the env-master x user-mode layer on top.

def _canonical(body: str = "what time?") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=0, prospect_id=0,
        state="message_replied", provider="unipile",
        provider_lead_id="li_maya_123",
        ts=datetime.now(timezone.utc), body=body, raw={},
    )


def _decision() -> ReplyDecision:
    return ReplyDecision(classification="clarifying",
                         draft_text="Dinner is at 7pm.",
                         reasoning="clarifying question")


def _run_ai_reply(db, prospect):
    with patch("backend.routes.webhooks.decide_reply",
               return_value=_decision()), \
         patch("backend.routes.webhooks.should_auto_send",
               return_value=True):
        return _handle_ai_reply(db, get_provider(), prospect, _canonical())


def test_auto_reply_fires_when_env_on_and_mode_auto(db, monkeypatch):
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    _u, _ev, p = _seed(db, autonomy="auto")
    result = _run_ai_reply(db, p)
    assert result["action"] == "auto_sent"
    assert db.query(models.PendingReply).count() == 0
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert "auto_reply_sent" in states


@pytest.mark.parametrize("mode", ["off", "ask"])
def test_auto_reply_queues_when_mode_not_auto(db, monkeypatch, mode):
    """Non-auto modes keep the existing staging behavior: the draft lands as
    a PendingReply for approval even with the env master on."""
    monkeypatch.setenv("SURPLUS_AUTOMATED_SENDS", "true")
    _u, _ev, p = _seed(db, autonomy=mode)
    result = _run_ai_reply(db, p)
    assert result["action"] == "queued"
    pending = db.query(models.PendingReply).one()
    assert pending.status == "pending"
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert "auto_reply_sent" not in states


def test_auto_reply_queues_when_env_master_off(db, monkeypatch):
    """The env master stays the ops kill switch: mode 'auto' alone is not
    enough for an unattended reply."""
    monkeypatch.delenv("SURPLUS_AUTOMATED_SENDS", raising=False)
    _u, _ev, p = _seed(db, autonomy="auto")
    result = _run_ai_reply(db, p)
    assert result["action"] == "queued"
    assert db.query(models.PendingReply).count() == 1


# ── pending queue + send / skip ───────────────────────────────────────────

def test_pending_lists_due_held_rows_for_owner_only(db):
    user, _ev, p = _seed(db, autonomy="ask")
    row = _stage_due(db, p)
    stranger = models.User(email="stranger@example.com")
    db.add(stranger); db.commit()

    mine = followups_route.list_pending_followups(db=db, user=user)
    assert [x.id for x in mine] == [row.id]
    assert mine[0].name == "Maya Rodriguez"
    assert mine[0].message == row.body
    assert followups_route.list_pending_followups(db=db, user=stranger) == []


def test_pending_excludes_future_and_resolved_rows(db):
    user, _ev, p = _seed(db, autonomy="ask")
    row = stage_followup(db, p)  # send_at in the future: not due, not listed
    assert followups_route.list_pending_followups(db=db, user=user) == []
    row.send_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row.status = "cancelled"     # resolved rows never resurface
    db.commit()
    assert followups_route.list_pending_followups(db=db, user=user) == []


def test_send_confirm_flips_pending_row_to_sent(db):
    user, _ev, p = _seed(db, autonomy="ask")
    row = _stage_due(db, p)
    out = followups_route.send_followup_now(row.id, db=db, user=user)
    assert out.status == "sent"
    assert followups_route.list_pending_followups(db=db, user=user) == []
    states = [o.state for o in db.get(models.Prospect, p.id).outreach]
    assert "follow_up_sent" in states


def test_skip_cancels_with_reason_skipped(db):
    user, _ev, p = _seed(db, autonomy="ask")
    row = _stage_due(db, p)
    out = followups_route.skip_followup(row.id, db=db, user=user)
    assert out.status == "cancelled"
    assert out.cancel_reason == "skipped"
    assert followups_route.list_pending_followups(db=db, user=user) == []


def test_skip_is_owner_scoped_404(db):
    _user, _ev, p = _seed(db, autonomy="ask")
    row = _stage_due(db, p)
    stranger = models.User(email="stranger@example.com")
    db.add(stranger); db.commit()
    with pytest.raises(HTTPException) as exc:
        followups_route.skip_followup(row.id, db=db, user=stranger)
    assert exc.value.status_code == 404
    db.expire_all()
    assert db.get(models.ScheduledFollowup, row.id).status == "scheduled"


def test_skip_conflicts_on_already_resolved_row(db):
    user, _ev, p = _seed(db, autonomy="ask")
    row = _stage_due(db, p)
    followups_route.skip_followup(row.id, db=db, user=user)
    with pytest.raises(HTTPException) as exc:
        followups_route.skip_followup(row.id, db=db, user=user)
    assert exc.value.status_code == 409
