"""tests/test_funnel_events.py : backend/observe/funnel.py logs the six
funnel stages (signal_detected -> matched_practice -> matched_signal_library
-> relationship_context -> high_confidence -> sent) as durable FunnelEvent
rows, at the two real points they happen (relationship_watch._emit for a
freshly detected signal; pipeline/send/sender.send_and_log for a confirmed
LinkedIn send) -- so a future funnel-UI PR reads a table instead of
reconstructing history.

Meta shapes below mirror updates_engine.apply_profile/apply_posts exactly
(see test_observe_production_signal_mapping.py, which established these as
the real production shapes), not backend/demo/cohort.py's synthetic keys --
the whole point of production_signal_category is scoring THESE.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.observe import funnel
from backend.agents.relationship.relationship_watch import _emit


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _make_user(db, *, practice_area=None, email="lawyer@example.com"):
    u = models.User(name="Jane", email=email, password_hash="h", practice_area=practice_area)
    db.add(u)
    db.flush()
    return u


def _make_contact(db, user, *, name="Contact", company=None, identity="k1"):
    c = models.Contact(user_id=user.id, primary_identity_key=identity, name=name, company=company)
    db.add(c)
    db.flush()
    return c


def _stages(db, contact_id=None):
    q = select(models.FunnelEvent)
    if contact_id is not None:
        q = q.where(models.FunnelEvent.contact_id == contact_id)
    return {e.stage for e in db.execute(q).scalars().all()}


# ── record_signal_funnel, via the real _emit() call site ────────────────────

def test_emit_always_logs_signal_detected(db):
    user = _make_user(db)
    contact = _make_contact(db, user)
    change = _emit(db, contact, "job_change", "Joined Acme", {"new_title": "VP of Sales"})
    stages = _stages(db, contact.id)
    assert funnel.SIGNAL_DETECTED in stages
    row = db.execute(select(models.FunnelEvent)
                     .where(models.FunnelEvent.stage == funnel.SIGNAL_DETECTED)).scalar_one()
    assert row.interaction_id == change["ri_id"]
    assert row.user_id == user.id


def test_emit_logs_matched_practice_and_signal_library_on_real_affinity(db):
    # corporate_ma x gc_appointment = 0.85 in SIGNAL_AFFINITY_SEED -- a real,
    # above-neutral match, reached via production_signal_category (no
    # demo-only signal_category key in this meta, on purpose).
    user = _make_user(db, practice_area="corporate_ma")
    contact = _make_contact(db, user)
    _emit(db, contact, "job_change", "Joined Acme as General Counsel",
         {"new_title": "General Counsel", "new_company": "Acme", "source": "brightdata"})
    stages = _stages(db, contact.id)
    assert funnel.MATCHED_PRACTICE in stages
    assert funnel.MATCHED_SIGNAL_LIBRARY in stages
    row = db.execute(select(models.FunnelEvent)
                     .where(models.FunnelEvent.stage == funnel.MATCHED_PRACTICE)).scalar_one()
    assert row.value == pytest.approx(0.85)


def test_emit_does_not_log_matched_practice_when_no_category_resolves(db):
    # No new_title -> production_signal_category returns None -> affinity()
    # is the neutral 0.5 fallback, not a match.
    user = _make_user(db, practice_area="corporate_ma")
    contact = _make_contact(db, user)
    _emit(db, contact, "job_change", "Something changed", {})
    stages = _stages(db, contact.id)
    assert funnel.MATCHED_PRACTICE not in stages
    assert funnel.MATCHED_SIGNAL_LIBRARY not in stages


def test_emit_logs_relationship_context_when_shared_company_exists(db):
    user = _make_user(db, practice_area="corporate_ma")
    _make_contact(db, user, name="Other", company="Acme", identity="k-other")
    contact = _make_contact(db, user, name="Target", company="Acme", identity="k-target")
    _emit(db, contact, "job_change", "Joined Acme as General Counsel",
         {"new_title": "General Counsel", "new_company": "Acme", "source": "brightdata"})
    assert funnel.RELATIONSHIP_CONTEXT in _stages(db, contact.id)


def test_emit_does_not_log_relationship_context_with_no_prior_signal(db):
    user = _make_user(db, practice_area="corporate_ma")
    contact = _make_contact(db, user, name="Target", company=None)
    _emit(db, contact, "job_change", "Joined Acme as General Counsel",
         {"new_title": "General Counsel", "new_company": "Acme", "source": "brightdata"})
    assert funnel.RELATIONSHIP_CONTEXT not in _stages(db, contact.id)


def test_emit_never_raises_even_if_downstream_scoring_breaks(db, monkeypatch):
    """funnel.record_signal_funnel is best-effort: _emit must still return the
    signal (and the caller's autodraft must still run) even if a scoring
    step inside funnel.py throws."""
    import backend.demo.ranking_trace as rt

    def _boom(*a, **kw):
        raise RuntimeError("scoring exploded")
    monkeypatch.setattr(rt, "compute_trace", _boom)

    user = _make_user(db, practice_area="corporate_ma")
    contact = _make_contact(db, user)
    change = _emit(db, contact, "job_change", "Joined Acme as General Counsel",
                   {"new_title": "General Counsel", "new_company": "Acme"})
    assert change["ri_id"] is not None
    # Earlier stages (computed before the patched call) still logged.
    assert funnel.SIGNAL_DETECTED in _stages(db, contact.id)
    assert funnel.HIGH_CONFIDENCE not in _stages(db, contact.id)


# ── record_sent ───────────────────────────────────────────────────────────

def test_record_sent_logs_for_linkedin_channel(db):
    user = _make_user(db)
    contact = _make_contact(db, user)
    funnel.record_sent(db, user_id=user.id, contact_id=contact.id, channel="linkedin")
    db.flush()
    assert funnel.SENT in _stages(db, contact.id)


def test_record_sent_ignores_email_channel(db):
    user = _make_user(db)
    contact = _make_contact(db, user)
    funnel.record_sent(db, user_id=user.id, contact_id=contact.id, channel="email")
    db.flush()
    assert funnel.SENT not in _stages(db, contact.id)


# ── rollup ────────────────────────────────────────────────────────────────

def test_rollup_zero_fills_every_stage(db):
    user = _make_user(db)
    from datetime import datetime, timezone
    counts = funnel.rollup(db, user.id, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert counts == {stage: 0 for stage in funnel.STAGES}


def test_rollup_counts_real_events(db):
    user = _make_user(db, practice_area="corporate_ma")
    contact = _make_contact(db, user)
    _emit(db, contact, "job_change", "Joined Acme as General Counsel",
         {"new_title": "General Counsel", "new_company": "Acme"})
    funnel.record_sent(db, user_id=user.id, contact_id=contact.id, channel="linkedin")
    db.flush()

    from datetime import datetime, timezone
    counts = funnel.rollup(db, user.id, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert counts[funnel.SIGNAL_DETECTED] == 1
    assert counts[funnel.MATCHED_PRACTICE] == 1
    assert counts[funnel.SENT] == 1
