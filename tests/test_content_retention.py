"""Summarize-then-expire content retention (retention.run_content_retention).

The invariants under test:
  - a body expires ONLY when older than the window AND beyond keep-last-N
  - nothing expires before the contact's thread_summary fact exists
  - the metadata skeleton (occurred_at/direction/type) survives; only the
    text payload is blanked
  - notes and activity_updates are never touched
  - dry-run (and the master switch being off) write nothing
In-memory SQLite; the LLM summarizer is monkeypatched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, retention
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _now():
    return datetime.now(timezone.utc)


def _setup(db, *, n_messages=30, oldest_days_ago=400):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:p", name="Pat")
    db.add(c); db.commit()
    # n messages, oldest first, spread from oldest_days_ago to now.
    step = oldest_days_ago / max(1, n_messages - 1)
    for i in range(n_messages):
        db.add(models.RelationshipInteraction(
            actor_user_id=u.id, contact_id=c.id, source_type="linkedin_dm",
            interaction_type="message",
            direction="outbound" if i % 2 else "inbound",
            occurred_at=_now() - timedelta(days=oldest_days_ago - i * step),
            title="Message", summary=f"message body {i}", meta_json="{}"))
    # A note and an activity_update: never expirable.
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="manual_note",
        interaction_type="note", direction="none",
        occurred_at=_now() - timedelta(days=oldest_days_ago),
        title="Note", summary="host's precious note", meta_json="{}"))
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="activity_update",
        interaction_type="new_post", direction="none",
        occurred_at=_now() - timedelta(days=oldest_days_ago),
        title="Update", summary="raised a round", meta_json="{}"))
    db.commit()
    return u, c


def _patch_summarizer(monkeypatch, works=True):
    """Make the rolling summary deterministic: write (or refuse to write) the
    thread_summary fact without an LLM."""
    def fake(db, contact, keep_n):
        if not works:
            return False
        from backend.agents.relationship.spine.memory import upsert_fact
        upsert_fact(db, contact.user_id, contact.id, "thread_summary",
                    "compressed history", source="thread_compress",
                    commit=False)
        return True
    monkeypatch.setattr(retention, "_refresh_thread_summary", fake)


def _bodies(db, c):
    rows = (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=c.id, interaction_type="message")
            .order_by(models.RelationshipInteraction.occurred_at).all())
    return [(r.summary or "") for r in rows]


def test_disabled_without_days_env(db, monkeypatch):
    monkeypatch.delenv("SURPLUS_CONTENT_RETENTION_DAYS", raising=False)
    res = retention.run_content_retention(db)
    assert res["enabled"] is False


def test_dry_run_reports_but_writes_nothing(db, monkeypatch):
    _setup(db)
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.setenv("SURPLUS_CONTENT_KEEP_LAST_N", "20")
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    _patch_summarizer(monkeypatch)
    res = retention.run_content_retention(db, dry_run=True)
    assert res["dry_run"] is True
    assert res["bodies_expirable"] > 0
    assert res["bodies_expired"] == 0
    c = db.query(models.Contact).first()
    assert all(b for b in _bodies(db, c))       # every body intact


def test_master_switch_off_forces_dry_run(db, monkeypatch):
    _setup(db)
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.delenv("SURPLUS_RETENTION_ENABLED", raising=False)
    _patch_summarizer(monkeypatch)
    res = retention.run_content_retention(db, dry_run=False)
    assert res["dry_run"] is True               # write demoted to report
    assert res["bodies_expired"] == 0


def test_expires_old_beyond_keep_n_only(db, monkeypatch):
    u, c = _setup(db, n_messages=30, oldest_days_ago=400)
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.setenv("SURPLUS_CONTENT_KEEP_LAST_N", "20")
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    _patch_summarizer(monkeypatch)
    res = retention.run_content_retention(db, dry_run=False)
    assert res["bodies_expired"] > 0
    bodies = _bodies(db, c)                      # oldest-first
    # The last 20 by recency are ALWAYS intact.
    assert all(b for b in bodies[-20:])
    # Expired rows are only in the older remainder, and each expired row is
    # both beyond keep-N and older than the cutoff.
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    rows = (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=c.id, interaction_type="message")
            .order_by(models.RelationshipInteraction.occurred_at).all())
    for r in rows:
        if (r.summary or "") == "":
            when = r.occurred_at.replace(tzinfo=timezone.utc)
            assert when < cutoff
            assert r.meta_json == retention._EXPIRED_META
            assert r.occurred_at is not None     # skeleton survives
            assert r.direction in ("inbound", "outbound")
    # Newer-than-cutoff bodies beyond keep-N also survive.
    kept_old = [r for r in rows if (r.summary or "") and
                r.occurred_at.replace(tzinfo=timezone.utc) >= cutoff]
    assert kept_old


def test_notes_and_updates_never_touched(db, monkeypatch):
    u, c = _setup(db)
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    _patch_summarizer(monkeypatch)
    retention.run_content_retention(db, dry_run=False)
    note = (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=c.id, interaction_type="note").one())
    upd = (db.query(models.RelationshipInteraction)
           .filter_by(contact_id=c.id, interaction_type="new_post").one())
    assert note.summary == "host's precious note"
    assert upd.summary == "raised a round"


def test_no_expiry_when_summary_refresh_fails(db, monkeypatch):
    u, c = _setup(db)
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    _patch_summarizer(monkeypatch, works=False)
    res = retention.run_content_retention(db, dry_run=False)
    assert res["bodies_expired"] == 0
    assert res["contacts_skipped_no_summary"] == 1
    assert all(b for b in _bodies(db, c))        # nothing lost


def test_user_scoping(db, monkeypatch):
    u1, c1 = _setup(db)
    u2 = models.User(name="Other", email="o@x.com", unipile_account_id="a2")
    db.add(u2); db.commit()
    c2 = models.Contact(user_id=u2.id, primary_identity_key="li:q", name="Q")
    db.add(c2); db.commit()
    db.add(models.RelationshipInteraction(
        actor_user_id=u2.id, contact_id=c2.id, source_type="linkedin_dm",
        interaction_type="message", direction="inbound",
        occurred_at=_now() - timedelta(days=300),
        title="Message", summary="other user's old body", meta_json="{}"))
    db.commit()
    monkeypatch.setenv("SURPLUS_CONTENT_RETENTION_DAYS", "180")
    monkeypatch.setenv("SURPLUS_CONTENT_KEEP_LAST_N", "20")
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    _patch_summarizer(monkeypatch)
    res = retention.run_content_retention(db, user_id=u1.id, dry_run=False)
    assert res["bodies_expired"] > 0
    other = (db.query(models.RelationshipInteraction)
             .filter_by(contact_id=c2.id).one())
    assert other.summary == "other user's old body"   # out of scope, intact
