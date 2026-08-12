"""tests/test_observe_trace_and_adapters.py : the universal DecisionTrace
contract itself (backend/observe/trace.py) and the object-specific adapters
(backend/observe/adapters.py) that produce it -- one shape for every
observable object type, per this package's own docstring.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort
from backend.observe import adapters
from backend.observe.trace import DecisionTrace, TraceFeature, DEMO, OBSERVED, SYNTHETIC


def test_decision_trace_rejects_unknown_provenance():
    with pytest.raises(ValueError):
        DecisionTrace(account_id=1, object_type="signal", object_id="1",
                      timestamp=None, provenance="not_a_real_value")


def test_decision_trace_to_dict_shape():
    trace = DecisionTrace(
        account_id=1, object_type="opportunity", object_id="42", timestamp=None,
        features=[TraceFeature(name="x", value=0.5, weight=0.2, evidence={"k": "v"})],
        decision="ranked #1", score=0.87, rank=1, provenance=DEMO,
        evaluation_references=[{"harness_id": "ablation", "version": "v1"}],
    )
    d = trace.to_dict()
    for key in ("account_id", "object_type", "object_id", "timestamp", "inputs", "entities",
                "features", "evidence", "decision", "score", "rank", "constraints",
                "policy_result", "versions", "outcome", "provenance", "evaluation_references"):
        assert key in d
    assert d["features"][0] == {"name": "x", "value": 0.5, "evidence": {"k": "v"}, "weight": 0.2}
    assert d["provenance"] == DEMO


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


@pytest.fixture
def cohort_data(db):
    cohort_id = cohort.generate(db, n_lawyers=8, days=30)
    user = db.execute(select(models.User)).scalars().first()
    contacts = db.execute(select(models.Contact).where(
        models.Contact.user_id == user.id)).scalars().all()
    draft = db.execute(select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.title.like("Drafted follow-up%"))).scalars().first()
    return cohort_id, user, contacts, draft


def test_every_adapter_returns_a_valid_decision_trace(db, cohort_data):
    cohort_id, user, contacts, draft = cohort_data
    contact = contacts[0]

    traces = [
        adapters.targeting_trace(db, user, contact),
        adapters.relationship_trace(db, user, contact),
        adapters.opportunity_trace(db, user, contact, contacts),
        adapters.jurisdiction_trace(db, user, contact),
        adapters.signal_library_trace(db, user.id, cohort_id, "acquisition_announced"),
    ]
    if draft is not None:
        traces.append(adapters.signal_trace(db, draft))
        traces.append(adapters.draft_trace(db, draft))
        traces.append(adapters.outcome_trace(db, draft))

    for t in traces:
        assert isinstance(t, DecisionTrace)
        d = t.to_dict()
        assert d["provenance"] in ("observed", "demo", "synthetic")
        assert d["object_type"]
        assert d["versions"]  # never empty -- every trace is version-stamped


def test_demo_user_traces_are_provenance_demo(db, cohort_data):
    _cohort_id, user, contacts, _draft = cohort_data
    assert user.is_demo is True
    trace = adapters.targeting_trace(db, user, contacts[0])
    assert trace.provenance == DEMO


def test_real_non_demo_user_traces_are_provenance_observed(db):
    user = models.User(email="real@example.com", name="Real Lawyer", is_demo=False,
                       practice_area="litigation")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="real:1", name="Real Contact")
    db.add(contact)
    db.commit()

    trace = adapters.relationship_trace(db, user, contact)
    assert trace.provenance == OBSERVED


def test_opportunity_trace_rank_matches_rank_opportunities(db, cohort_data):
    from backend.demo import ranking_trace as rt
    _cohort_id, user, contacts, _draft = cohort_data
    ranked = rt.rank_opportunities(db, user, contacts)
    target = contacts[len(contacts) // 2]
    expected_rank = next(t.rank for t in ranked if t.contact_id == target.id)
    trace = adapters.opportunity_trace(db, user, target, contacts)
    assert trace.rank == expected_rank


def test_jurisdiction_trace_decision_matches_solicitation_signals_check(db, cohort_data):
    from backend.agents.relationship import solicitation_signals as sig
    _cohort_id, user, contacts, _draft = cohort_data
    contact = contacts[0]
    verdict = sig.check(db, user, contact, "linkedin_dm")
    trace = adapters.jurisdiction_trace(db, user, contact, "linkedin_dm")
    assert trace.decision == ("PASS" if verdict.allowed else "BLOCK")
    assert trace.policy_result["allowed"] == verdict.allowed
