"""Tests for backend/routes/curation.py : the HTTP surface.

Calls route handlers directly (like test_triage_routes.py) to dodge the
TestClient path. Exercises the LIVE endpoints end-to-end plus the
feature-flag gating for NEAR-TERM.
"""
from __future__ import annotations
import io
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def user_and_event(db):
    user = models.User(name="Op", email="op@x.com", unipile_account_id=None)
    db.add(user); db.flush()
    ev = models.Event(
        user_id=user.id, role="ML engineers", seniority="Senior",
        co_stage="Seed", headcount=20, format="Mixer", city="SF",
        goal="Hiring pipeline", budget=5000, sources="linkedin",
    )
    db.add(ev); db.commit()
    return user, ev


def _upload(content: str | bytes) -> UploadFile:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return UploadFile(filename="x.csv", file=io.BytesIO(content),
                      headers={"content-type": "text/csv"})


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """All tests run with ANTHROPIC_API_KEY unset so we hit the offline paths."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ── ingest + scoring round trip ────────────────────────────────────────

def test_preview_then_import_flow(db, user_and_event):
    import asyncio
    from backend.routes.curation import preview_mapping, import_attendees, ImportBody
    user, ev = user_and_event
    csv = "Name,Email,Title,Company\nAlice,a@x.com,Staff Eng,Acme\n"
    preview = asyncio.run(preview_mapping(ev.id, _upload(csv), db, user))
    assert "name" in preview.mapping
    assert preview.row_count == 1

    body = ImportBody(csv=csv, mapping=preview.mapping, list_source="alumni")
    result = import_attendees(ev.id, body, db, user)
    assert result.inserted == 1
    assert result.attendees[0].list_source == "alumni"
    assert result.attendees[0].name == "Alice"


def test_score_endpoint_writes_score_and_rule_trace(db, user_and_event):
    from backend.routes.curation import import_attendees, score_attendees, ImportBody
    user, ev = user_and_event
    csv = "Name,Email,Title\nAlice,a@x.com,Senior PM\n"
    import_attendees(
        ev.id, ImportBody(csv=csv,
                          mapping={"name": "Name", "email": "Email", "role": "Title"}),
        db, user,
    )
    result = score_attendees(ev.id, threshold=50, with_rationale=True, db=db, user=user)
    assert result.scored == 1
    assert result.method == "rule_based"
    a = db.query(models.Attendee).filter_by(event_id=ev.id).first()
    assert a.fit_score > 0
    assert json.loads(a.fit_rule_trace)  # non-empty list


def test_high_fit_filters_below_threshold(db, user_and_event):
    from backend.routes.curation import high_fit
    user, ev = user_and_event
    # Insert one high-fit + one low-fit attendee directly.
    db.add(models.Attendee(event_id=ev.id, name="Hi", email="h@x.com",
                           fit_score=80))
    db.add(models.Attendee(event_id=ev.id, name="Lo", email="l@x.com",
                           fit_score=30))
    db.commit()
    rows = high_fit(ev.id, threshold=70, limit=10, db=db, user=user)
    assert [r.name for r in rows] == ["Hi"]


def test_gap_analysis_endpoint(db, user_and_event):
    from backend.routes.curation import gap_analysis_endpoint, GapAnalysisBody
    user, ev = user_and_event
    db.add(models.Attendee(
        event_id=ev.id, name="A", email="a@x.com",
        enrichment=json.dumps({"role": {"function": "Engineering"}}),
    ))
    db.commit()
    body = GapAnalysisBody(
        target_distributions={"function": {"Engineering": 0.5, "Product": 0.5}},
        headcount=10,
    )
    out = gap_analysis_endpoint(ev.id, body, db, user)
    assert out["method"] == "rule_based"
    assert "function" in out["buckets"]


# ── intro recs ─────────────────────────────────────────────────────────

def test_build_and_export_intros(db, user_and_event):
    from backend.routes.curation import build_intros, get_intro_card
    user, ev = user_and_event
    e = models.Attendee(event_id=ev.id, name="Eng", email="e@x.com",
                        enrichment=json.dumps({"role": {"function": "Engineering"}}))
    p = models.Attendee(event_id=ev.id, name="Prod", email="p@x.com",
                        enrichment=json.dumps({"role": {"function": "Product"}}))
    db.add_all([e, p]); db.commit()

    summary = build_intros(ev.id, min_weight=0.1, max_per_attendee=6,
                            db=db, user=user)
    assert summary["intros"] >= 1
    assert summary["method"] == "rule_based"

    card = get_intro_card(ev.id, e.id, db=db, user=user)
    assert card["intro_count"] >= 1
    assert card["intros"][0]["method"] == "rule_based"


# ── outreach + follow-up + attribution ────────────────────────────────

def test_compose_outreach_template_fallback(db, user_and_event):
    from backend.routes.curation import compose_outreach
    user, ev = user_and_event
    a = models.Attendee(event_id=ev.id, name="Alice", email="a@x.com",
                        role="PM", company="Acme",
                        enrichment="{}")
    db.add(a); db.commit()
    res = compose_outreach(ev.id, a.id, slot="high_fit_invite", db=db, user=user)
    assert res["method"] == "template"
    assert "Alice" in res["body"]


def test_compose_outreach_rejects_unknown_slot(db, user_and_event):
    from backend.routes.curation import compose_outreach
    user, ev = user_and_event
    a = models.Attendee(event_id=ev.id, name="A", email="a@x.com")
    db.add(a); db.commit()
    with pytest.raises(HTTPException) as exc:
        compose_outreach(ev.id, a.id, slot="bogus", db=db, user=user)
    assert exc.value.status_code == 422


def test_log_and_list_follow_ups(db, user_and_event):
    from backend.routes.curation import (
        log_follow_up, list_follow_ups, FollowUpBody,
    )
    user, ev = user_and_event
    a = models.Attendee(event_id=ev.id, name="A", email="a@x.com")
    db.add(a); db.commit()
    body = FollowUpBody(kind="meeting", notes="Coffee chat, discussed roles.")
    row = log_follow_up(ev.id, a.id, body, db=db, user=user)
    assert row.id is not None
    assert row.kind == "meeting"
    rows = list_follow_ups(ev.id, a.id, db=db, user=user)
    assert len(rows) == 1


def test_attribution_writes_audit_log(db, user_and_event):
    from backend.routes.curation import (
        run_attribution, get_attribution, AttributionBody,
    )
    user, ev = user_and_event
    a = models.Attendee(event_id=ev.id, name="A", email="a@x.com")
    db.add(a); db.commit()
    result = run_attribution(
        ev.id, a.id, AttributionBody(operator_notes="Met at event, hired 3 months later."),
        db=db, user=user,
    )
    # No API key : outcome falls through to "none" but the row still persists.
    assert result.attendee_id == a.id
    # LLMCall should have a "disabled" entry
    rows = db.query(models.LLMCall).filter(
        models.LLMCall.attendee_id == a.id,
        models.LLMCall.purpose == "attribution",
    ).all()
    assert any(r.status == "disabled" for r in rows)
    # GET attribution works too
    got = get_attribution(ev.id, a.id, db=db, user=user)
    assert got is not None


# ── feature flag gating ────────────────────────────────────────────────

def test_get_features_returns_all_off_by_default(db, user_and_event, monkeypatch):
    from backend.routes.curation import get_features
    from backend.curation import features
    for name in features.NEAR_TERM_FEATURES:
        monkeypatch.delenv(f"SURPLUS_FEATURE_{name.upper()}", raising=False)
    user, ev = user_and_event
    res = get_features(ev.id, db=db, user=user)
    assert not any(res["flags"].values())


def test_near_term_endpoints_404_when_flag_off(db, user_and_event, monkeypatch):
    from backend.routes.curation import (
        near_term_sponsor_match, SponsorMatchBody,
    )
    monkeypatch.delenv("SURPLUS_FEATURE_SPONSOR_MATCH", raising=False)
    user, ev = user_and_event
    with pytest.raises(HTTPException) as exc:
        near_term_sponsor_match(
            ev.id, SponsorMatchBody(sponsor_profile={"name": "X"}),
            db=db, user=user,
        )
    assert exc.value.status_code == 404


def test_near_term_endpoint_works_when_flag_on(db, user_and_event, monkeypatch):
    from backend.routes.curation import (
        near_term_sponsor_match, SponsorMatchBody,
    )
    monkeypatch.setenv("SURPLUS_FEATURE_SPONSOR_MATCH", "1")
    user, ev = user_and_event
    a = models.Attendee(
        event_id=ev.id, name="A", email="a@x.com",
        enrichment=json.dumps({
            "role": {"function": "Engineering"},
            "seniority": {"level": "Senior"},
            "firmographic": {"company_industry": "ML"},
        }),
    )
    db.add(a); db.commit()
    res = near_term_sponsor_match(
        ev.id,
        SponsorMatchBody(sponsor_profile={
            "name": "Sponsor", "buyer_function": "Engineering",
            "buyer_seniority": ["Senior"], "industries": ["ML"],
        }),
        db=db, user=user,
    )
    assert res["matches"]
    assert res["matches"][0]["method"] == "rule_based"


def test_recognition_cross_reference_flags(db, user_and_event, monkeypatch):
    from backend.routes.curation import near_term_recognition, RecognitionBody
    monkeypatch.setenv("SURPLUS_FEATURE_PROPRIETARY_RECOGNITION", "1")
    user, ev = user_and_event
    a = models.Attendee(event_id=ev.id, name="Recognized Person",
                        email="r@x.com")
    db.add(a); db.commit()
    res = near_term_recognition(
        ev.id,
        RecognitionBody(entries=[
            {"email": "r@x.com", "list_name": "F100 CTOs"},
        ]),
        db=db, user=user,
    )
    assert res["flagged"] == 1
    db.refresh(a)
    flags = json.loads(a.recognition_flags)
    assert "F100 CTOs" in flags


def test_predict_no_show(db, user_and_event, monkeypatch):
    from backend.routes.curation import near_term_predict_no_show
    monkeypatch.setenv("SURPLUS_FEATURE_YIELD_PREDICTION", "1")
    user, ev = user_and_event
    db.add(models.Attendee(event_id=ev.id, name="X", email="x@x.com",
                           rsvp_status="waitlist"))
    db.add(models.Attendee(event_id=ev.id, name="Y", email="y@x.com",
                           rsvp_status="rsvp_yes"))
    db.commit()
    res = near_term_predict_no_show(ev.id, db=db, user=user)
    assert res["predicted"] == 2
    assert res["method"] == "rule_based"
    # The waitlist one should have higher probability than the rsvp_yes one
    by_id = {r["attendee_id"]: r["no_show_probability"] for r in res["results"]}
    rows = {a.name: a for a in db.query(models.Attendee).all()}
    assert by_id[rows["X"].id] > by_id[rows["Y"].id]


def test_enrich_all_detaches_instead_of_blocking(db, user_and_event):
    """enrich-all is a per-attendee Claude call; running it inline over a large
    list trips the edge timeout (524). The route must hand off to run_detached
    and return a fast ack, never loop over enrich_attendee in the request."""
    from unittest.mock import patch
    from backend.routes import curation
    user, ev = user_and_event
    for i in range(3):
        db.add(models.Attendee(event_id=ev.id, name=f"A{i}", email=f"a{i}@x.com"))
    db.commit()

    calls = []
    with patch.object(curation, "run_detached",
                      side_effect=lambda fn, *a, **k: calls.append((fn.__name__, a))), \
         patch.object(curation.enrichment_mod, "enrich_attendee") as inline:
        out = curation.enrich_all(ev.id, refresh=False, db=db, user=user)

    inline.assert_not_called()                 # no inline LLM loop in the request
    assert out == {"event_id": ev.id, "started": True, "total": 3}
    assert calls and calls[0][0] == "_run_enrich_all"
    assert calls[0][1] == (ev.id, False)


def test_run_enrich_all_is_fail_soft_per_row(db, user_and_event):
    """The detached body must not let one bad enrichment sink the batch."""
    from unittest.mock import patch
    from backend.routes import curation
    _user, ev = user_and_event
    for i in range(3):
        db.add(models.Attendee(event_id=ev.id, name=f"A{i}", email=f"a{i}@x.com"))
    db.commit()

    seen = []

    def flaky(_db, attendee, **kw):
        seen.append(attendee.id)
        if len(seen) == 2:
            raise RuntimeError("boom")

    with patch.object(curation.enrichment_mod, "enrich_attendee", side_effect=flaky):
        curation._run_enrich_all(db, ev.id, False)   # must not raise

    assert len(seen) == 3                       # all rows attempted despite one failure


def test_list_attendees_is_bounded_by_limit(db, user_and_event):
    """list_attendees must cap how many rows it materializes so a runaway import
    can't build an unbounded response in the request path. The cap is clamped to
    _ATTENDEE_LIST_MAX and rows come back fit-ranked."""
    from backend.routes.curation import list_attendees, _ATTENDEE_LIST_MAX
    user, ev = user_and_event
    for i in range(15):
        db.add(models.Attendee(event_id=ev.id, name=f"A{i}",
                               email=f"a{i}@x.com", fit_score=i))
    db.commit()

    top5 = list_attendees(ev.id, limit=5, db=db, user=user)
    assert len(top5) == 5
    assert top5[0].fit_score == 14           # fit-ranked, highest first

    # An absurd limit is clamped to the hard ceiling, never honored verbatim.
    everyone = list_attendees(ev.id, limit=10_000_000, db=db, user=user)
    assert len(everyone) == 15               # only 15 exist
    assert 10_000_000 > _ATTENDEE_LIST_MAX   # ceiling would have applied at scale
