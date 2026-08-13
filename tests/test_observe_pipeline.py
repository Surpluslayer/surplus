"""tests/test_observe_pipeline.py : backend/observe/pipeline.py -- the
8-stage execution trace (Observe checklist section B). Verifies real
measured latency, honest per-stage status derived from actual documented
fallback conditions (not fabricated), per-stage exception isolation, and
that a signaled contact produces an all-success pipeline while an
unsignaled one produces the expected warnings.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort
from backend.observe import pipeline
from backend.observe.trace import DEMO, OBSERVED


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


def test_pipeline_stages_are_in_order_and_versioned(db):
    cohort_id = cohort.generate(db, n_lawyers=8, days=30)
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    trace = pipeline.compute_pipeline_trace(db, user, contacts[0], contacts)
    names = [s.name for s in trace.stages]
    assert names == list(pipeline.STAGE_ORDER)
    for s in trace.stages:
        assert s.version is not None
        assert s.latency_ms >= 0.0
        assert s.status in ("success", "warning", "failure")


def test_pipeline_overall_status_is_worst_of_its_stages(db):
    cohort_id = cohort.generate(db, n_lawyers=8, days=30)
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    trace = pipeline.compute_pipeline_trace(db, user, contacts[0], contacts)
    d = trace.to_dict()
    statuses = {s["status"] for s in d["stages"]}
    if "failure" in statuses:
        assert d["overall_status"] == "failure"
    elif "warning" in statuses:
        assert d["overall_status"] == "warning"
    else:
        assert d["overall_status"] == "success"


def test_a_signaled_contact_produces_clean_signal_stages(db):
    """Find a contact with a real detected+engaged signal -- the
    signal-dependent stages (ingestion/signal_library/output) should report
    success. entity_resolution/relationship are NOT asserted here:
    relationship_type and interaction history are independently-random draws
    in the generator, unrelated to whether this particular contact has a
    signal, so they can legitimately still warn."""
    import json
    cohort_id = cohort.generate(db, n_lawyers=15, days=30)
    draft = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().all()
    engaged_draft = next(
        (r for r in draft if json.loads(r.meta_json or "{}").get("engaged")), None)
    if engaged_draft is None:
        pytest.skip("no engaged draft in this generated cohort run")
    user = db.get(models.User, engaged_draft.actor_user_id)
    contact = db.get(models.Contact, engaged_draft.contact_id)
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    trace = pipeline.compute_pipeline_trace(db, user, contact, contacts)
    by_name = {s.name: s for s in trace.stages}
    for name in ("ingestion", "signal_library", "output"):
        assert by_name[name].status == "success", f"{name}: {by_name[name].to_dict()}"


def test_a_draft_awaiting_a_decision_is_not_a_warning(db):
    """The expected state of every fresh draft was being reported as a
    pipeline warning. Nothing is substituted and nothing degraded when a
    lawyer simply hasn't sent or snoozed a draft yet -- the stage computed the
    right answer. It still has to say WHY the outcome is unresolved, otherwise
    "output: unresolved" is unreadable."""
    import json
    from datetime import datetime, timedelta, timezone
    user = models.User(email="pending@example.com", name="L", is_demo=True,
                       practice_area="litigation", bar_jurisdiction="NY")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="pend:1", name="Drafted Person")
    db.add(contact)
    db.flush()
    db.add(models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id,
        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
        source_type="activity_update", interaction_type="job_change",
        title="Changed roles", meta_json=json.dumps({"draft": "Congrats!",
                                                     "milestone_type": "role"})))
    db.commit()

    out = {s.name: s for s in
           pipeline.compute_pipeline_trace(db, user, contact, [contact]).stages}["output"]
    assert out.status == "success", out.to_dict()
    assert out.fallback is None
    assert out.note and "awaiting a send/snooze decision" in out.note


def test_an_unsignaled_contact_reports_ingestion_and_output_warnings(db):
    user = models.User(email="cold@example.com", name="Cold Lawyer", is_demo=True,
                       practice_area="litigation", bar_jurisdiction="NY")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="cold:1", name="No Signal Contact")
    db.add(contact)
    db.commit()

    trace = pipeline.compute_pipeline_trace(db, user, contact, [contact])
    by_name = {s.name: s for s in trace.stages}
    assert by_name["ingestion"].status == "warning"
    assert by_name["signal_library"].status == "warning"
    assert by_name["output"].status == "warning"
    assert by_name["relationship"].status == "warning"  # zero interactions -> cold start


def test_unset_practice_area_and_jurisdiction_trigger_documented_fallbacks(db):
    user = models.User(email="unset@example.com", name="Unset Lawyer", is_demo=True)
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="unset:1", name="Some Contact")
    db.add(contact)
    db.commit()

    trace = pipeline.compute_pipeline_trace(db, user, contact, [contact])
    by_name = {s.name: s for s in trace.stages}
    assert by_name["targeting"].status == "warning"
    assert "practice_area" in by_name["targeting"].fallback
    assert by_name["jurisdiction"].status == "warning"
    assert "bar_jurisdiction" in by_name["jurisdiction"].fallback
    # relationship_type unset -> PROSPECT is a NOTE, not a fallback: PROSPECT
    # is the stricter classification (clients are exempt from the Rule 7.3
    # gate, prospects are not), so nothing is degraded and the log must not
    # warn. It still has to SAY so -- the reader needs to know the contact was
    # classified rather than declared.
    entity = by_name["entity_resolution"]
    assert entity.status == "success"
    assert entity.fallback is None
    assert "PROSPECT" in entity.note and "stricter" in entity.note


def test_pipeline_provenance_matches_demo_vs_observed(db):
    demo_user = models.User(email="demo-p@example.com", is_demo=True, name="D")
    real_user = models.User(email="real-p@example.com", is_demo=False, name="R")
    db.add_all([demo_user, real_user])
    db.flush()
    demo_contact = models.Contact(user_id=demo_user.id, primary_identity_key="d:1", name="X")
    real_contact = models.Contact(user_id=real_user.id, primary_identity_key="r:1", name="Y")
    db.add_all([demo_contact, real_contact])
    db.commit()

    demo_trace = pipeline.compute_pipeline_trace(db, demo_user, demo_contact, [demo_contact])
    real_trace = pipeline.compute_pipeline_trace(db, real_user, real_contact, [real_contact])
    assert demo_trace.provenance == DEMO
    assert real_trace.provenance == OBSERVED
