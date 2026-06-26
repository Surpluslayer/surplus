"""Tests for the sponsor-matching extension to Stage 04.

Exercises the heuristic sponsor scorer (no LLM), the /match route's
sponsor-match persistence, the read-back via /matches, the
/pairs/explain extension that accepts a sponsor side, and the new
SPONSOR column on the ROI ledger.
"""
from __future__ import annotations
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, schemas
from backend.db import Base
from backend.agents.events import roi as roi_agent
from backend.agents.events.sponsor_matcher import (
    parse_buyer_profile,
    score_event_sponsors,
    score_sponsor_vs_prospect,
)


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
    u = models.User(name="Op", email="op@x.com", unipile_account_id=None)
    db.add(u); db.flush()
    ev = models.Event(
        user_id=u.id, role="ML platform engineers", seniority="Senior",
        co_stage="Seed", headcount=20, format="Sit-down dinner",
        city="SF", goal="Hiring pipeline", budget=8000, sources="linkedin",
    )
    db.add(ev); db.commit()
    return u, ev


def _make_prospect(db, event, **kw):
    p = models.Prospect(
        event_id=event.id,
        identity=kw.get("identity", kw.get("name", "x")).lower().replace(" ", "-"),
        name=kw.get("name", "Tester"),
        role=kw.get("role", "Senior ML engineer"),
        company=kw.get("company", "Acme"),
        seniority=kw.get("seniority", "Senior"),
        side=kw.get("side", "Builds"),
        works_on=kw.get("works_on", "ml-platform"),
        offers=kw.get("offers", "ml infra"),
        seeks=kw.get("seeks", "hiring leads"),
        fit_score=kw.get("fit_score", 80),
        status=kw.get("status", "rsvp"),
        li_resolved=True,
    )
    db.add(p); db.commit()
    return p


# ── parse_buyer_profile ────────────────────────────────────────────────

def test_parse_buyer_profile_handles_json_string():
    raw = json.dumps({"target_role": "ML engineer", "industry": "ml-platform"})
    out = parse_buyer_profile(raw)
    assert out["target_role"] == "ML engineer"
    assert out["industry"] == "ml-platform"
    assert out["intent"] == "buying"  # default


def test_parse_buyer_profile_handles_dict_input():
    out = parse_buyer_profile({"seniority": "Senior", "intent": "sponsoring"})
    assert out["seniority"] == "Senior"
    assert out["intent"] == "sponsoring"
    assert out["target_role"] == ""


def test_parse_buyer_profile_handles_garbage():
    assert parse_buyer_profile(None)["intent"] == "buying"
    assert parse_buyer_profile("")["intent"] == "buying"
    assert parse_buyer_profile("not-json")["intent"] == "buying"


# ── heuristic scorer ───────────────────────────────────────────────────

def test_full_alignment_scores_high(db, user_and_event):
    _u, ev = user_and_event
    p = _make_prospect(db, ev, role="Senior ML platform engineer",
                       works_on="ml-platform", seniority="Senior")
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere", tier="gold",
        buyer_profile=json.dumps({
            "target_role": "ML platform engineer",
            "seniority": "Senior", "industry": "ml-platform",
            "company_stage": "Seed", "intent": "buying",
        }),
    )
    db.add(sponsor); db.commit()
    score, reasons = score_sponsor_vs_prospect(sponsor, p, ev)
    assert score > 70
    # reasons fired for each axis
    joined = " ".join(reasons).lower()
    assert "role" in joined and "seniority" in joined
    assert "industry" in joined and "stage" in joined
    # Asymmetry / intent reason is first
    assert "buying" in reasons[0].lower()


def test_misaligned_role_falls_below_floor(db, user_and_event):
    _u, ev = user_and_event
    p = _make_prospect(db, ev, role="Marketing manager",
                       works_on="brand-strategy", seniority="Junior")
    sponsor = models.Sponsor(
        event_id=ev.id, name="Datadog",
        buyer_profile=json.dumps({"target_role": "platform engineer",
                                   "seniority": "Staff+",
                                   "industry": "observability"}),
    )
    db.add(sponsor); db.commit()
    score, _ = score_sponsor_vs_prospect(sponsor, p, ev)
    # No axis matches : raw signal is 0 + tiebreak only.
    assert score < 10


def test_score_event_sponsors_respects_top_k_and_threshold(db, user_and_event):
    _u, ev = user_and_event
    # one strong + one weak attendee
    p_strong = _make_prospect(db, ev, name="Strong", role="Senior ML platform engineer",
                              works_on="ml-platform", seniority="Senior",
                              fit_score=88)
    p_weak = _make_prospect(db, ev, name="Weak", role="Marketing",
                            works_on="brand", seniority="Junior",
                            fit_score=20)
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere",
        buyer_profile=json.dumps({"target_role": "ML platform engineer",
                                   "seniority": "Senior",
                                   "industry": "ml-platform"}),
    )
    db.add(sponsor); db.commit()
    db.refresh(ev)
    scored = score_event_sponsors(ev, [p_strong, p_weak])
    rows = scored[sponsor.id]
    # weak attendee falls below the min_score threshold
    assert [r["prospect_id"] for r in rows] == [p_strong.id]


def test_no_sponsors_returns_empty(db, user_and_event):
    _u, ev = user_and_event
    p = _make_prospect(db, ev)
    assert score_event_sponsors(ev, [p]) == {}


# ── route integration ──────────────────────────────────────────────────

def test_event_create_persists_sponsors(db, user_and_event, monkeypatch):
    from backend.routes.events import create_event
    user, _ = user_and_event
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = schemas.EventCreate(
        role="ML eng", seniority=["Senior"], co_stage=["Seed"],
        headcount=20, format="Sit-down dinner", city="SF",
        goal=["Hiring pipeline"], budget=5000,
        sponsors=[
            schemas.SponsorIn(
                name="Cohere", tier="gold",
                buyer_profile=schemas.SponsorBuyerProfile(
                    target_role="ML engineer", seniority="Senior",
                    industry="ml-platform",
                ),
            ),
            schemas.SponsorIn(name="Notion", tier="silver"),
        ],
    )
    out = create_event(payload, db, user)
    assert len(out.sponsors) == 2
    names = sorted(s.name for s in out.sponsors)
    assert names == ["Cohere", "Notion"]
    cohere = next(s for s in out.sponsors if s.name == "Cohere")
    assert cohere.buyer_profile.target_role == "ML engineer"
    assert cohere.buyer_profile.intent == "buying"


def test_match_route_returns_and_persists_sponsor_matches(db, user_and_event, monkeypatch):
    from backend.routes.matching import match as match_route
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    p1 = _make_prospect(db, ev, name="Maya Rodriguez",
                        role="Senior ML platform engineer",
                        works_on="ml-platform", seniority="Senior",
                        fit_score=84)
    p2 = _make_prospect(db, ev, name="Theo Lindqvist",
                        role="Staff observability engineer",
                        works_on="observability", seniority="Staff+",
                        fit_score=86, side="Hires",
                        offers="hiring leads", seeks="ml infra")
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere", tier="gold",
        buyer_profile=json.dumps({
            "target_role": "ML platform engineer",
            "seniority": "Senior", "industry": "ml-platform",
        }),
    )
    db.add(sponsor); db.commit()
    db.refresh(ev)

    res = match_route(ev.id, db=db, user=user)
    # Sponsor matches surface in the response
    assert len(res.sponsor_matches) == 1
    block = res.sponsor_matches[0]
    assert block["sponsor_name"] == "Cohere"
    assert block["tier"] == "gold"
    assert block["matches"], "expected at least one match"
    top = block["matches"][0]
    assert top["prospect_name"] in {"Maya Rodriguez", "Theo Lindqvist"}
    assert top["score"] > 0
    assert isinstance(top["reasons"], list) and top["reasons"]
    # Persisted to DB
    rows = db.query(models.SponsorMatch).filter(
        models.SponsorMatch.sponsor_id == sponsor.id
    ).all()
    assert rows, "SponsorMatch rows should have been written"


def test_match_route_idempotent_for_sponsors(db, user_and_event, monkeypatch):
    from backend.routes.matching import match as match_route
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    _make_prospect(db, ev, name="A", role="Senior ML eng",
                   works_on="ml-platform")
    _make_prospect(db, ev, name="B", role="Staff platform eng",
                   works_on="ml-platform", side="Hires",
                   offers="hires", seeks="ml infra")
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere",
        buyer_profile=json.dumps({"target_role": "ML engineer",
                                   "industry": "ml-platform",
                                   "seniority": "Senior"}),
    )
    db.add(sponsor); db.commit()
    db.refresh(ev)

    match_route(ev.id, db=db, user=user)
    after_first = db.query(models.SponsorMatch).count()
    match_route(ev.id, db=db, user=user)
    after_second = db.query(models.SponsorMatch).count()
    assert after_first == after_second  # wiped + re-written, not duplicated


def test_match_route_with_no_sponsors_returns_empty_list(db, user_and_event, monkeypatch):
    from backend.routes.matching import match as match_route
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    _make_prospect(db, ev, name="A")
    _make_prospect(db, ev, name="B", side="Hires",
                   offers="hires", seeks="ml infra")
    res = match_route(ev.id, db=db, user=user)
    assert res.sponsor_matches == []


# ── explain endpoint ────────────────────────────────────────────────────

def test_explain_sponsor_vs_prospect(db, user_and_event, monkeypatch):
    from backend.routes.matching import (
        explain_pair_endpoint, ExplainRequest, match as match_route,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    p = _make_prospect(db, ev, name="Maya Rodriguez",
                       role="Senior ML platform engineer",
                       works_on="ml-platform", seniority="Senior")
    # Need ≥2 prospects for /match to not 409
    _make_prospect(db, ev, name="Theo", side="Hires",
                   offers="hires", seeks="ml infra",
                   works_on="observability")
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere",
        buyer_profile=json.dumps({"target_role": "ML platform engineer",
                                   "seniority": "Senior",
                                   "industry": "ml-platform"}),
    )
    db.add(sponsor); db.commit()
    db.refresh(ev)
    match_route(ev.id, db=db, user=user)

    res = explain_pair_endpoint(
        ev.id,
        ExplainRequest(a_id=sponsor.id, b_id=p.id,
                       a_kind="sponsor", b_kind="prospect"),
        db, user,
    )
    # No API key, but the structured fallback still produces text
    assert res.explanation
    # Source is "cached" (fallback) when there's no API key
    assert res.source in ("llm", "cached")
    # The fallback should mention the prospect name
    assert "Maya" in res.explanation or "Rodriguez" in res.explanation


# ── ROI sponsor column ──────────────────────────────────────────────────

def test_roi_ledger_carries_sponsor_attribution(db, user_and_event, monkeypatch):
    from backend.routes.matching import match as match_route
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    p1 = _make_prospect(db, ev, name="Maya",
                        role="Senior ML platform engineer",
                        works_on="ml-platform", seniority="Senior",
                        fit_score=92)
    p2 = _make_prospect(db, ev, name="Bob",
                        role="Senior data engineer",
                        works_on="data-infra", seniority="Senior",
                        side="Hires", offers="hires", seeks="data work",
                        fit_score=85)
    sponsor = models.Sponsor(
        event_id=ev.id, name="Cohere",
        buyer_profile=json.dumps({"target_role": "ML platform engineer",
                                   "seniority": "Senior",
                                   "industry": "ml-platform"}),
    )
    db.add(sponsor); db.commit()
    db.refresh(ev)
    match_route(ev.id, db=db, user=user)
    db.refresh(ev)

    ledger, _ = roi_agent.settle(ev, [p1, p2])
    by_name = {row["name"]: row for row in ledger}
    # Maya is the high-fit ML platform engineer, should be attributed to Cohere
    assert by_name["Maya"]["sponsor"] == "Cohere"


def test_roi_ledger_omits_sponsor_when_event_has_none(db, user_and_event):
    _u, ev = user_and_event
    p = _make_prospect(db, ev)
    ledger, _ = roi_agent.settle(ev, [p])
    assert ledger[0]["sponsor"] == ""


# ── sponsor CRUD endpoints ─────────────────────────────────────────────

def test_create_sponsor_endpoint_persists(db, user_and_event):
    from backend.routes.matching import create_sponsor, SponsorCreate
    user, ev = user_and_event
    body = SponsorCreate(
        name="Cohere", tier="gold",
        buyer_profile=schemas.SponsorBuyerProfile(
            target_role="ML platform engineer", seniority="Senior",
            industry="ml-platform",
        ),
    )
    out = create_sponsor(ev.id, body, db, user)
    assert out.name == "Cohere"
    assert out.buyer_profile.target_role == "ML platform engineer"
    rows = db.query(models.Sponsor).filter(
        models.Sponsor.event_id == ev.id
    ).all()
    assert len(rows) == 1


def test_create_sponsor_rejects_blank_name(db, user_and_event):
    from backend.routes.matching import create_sponsor, SponsorCreate
    user, ev = user_and_event
    with pytest.raises(Exception) as exc:
        create_sponsor(ev.id, SponsorCreate(name="  "), db, user)
    assert getattr(exc.value, "status_code", 0) == 422


def test_list_sponsors_returns_creation_order(db, user_and_event):
    from backend.routes.matching import create_sponsor, list_sponsors, SponsorCreate
    user, ev = user_and_event
    create_sponsor(ev.id, SponsorCreate(name="A"), db, user)
    create_sponsor(ev.id, SponsorCreate(name="B"), db, user)
    rows = list_sponsors(ev.id, db, user)
    assert [s.name for s in rows] == ["A", "B"]


def test_update_sponsor_patches_fields(db, user_and_event):
    from backend.routes.matching import (
        create_sponsor, update_sponsor, SponsorCreate, SponsorUpdate,
    )
    user, ev = user_and_event
    created = create_sponsor(
        ev.id, SponsorCreate(name="Cohere", tier="silver"), db, user,
    )
    out = update_sponsor(
        ev.id, created.id,
        SponsorUpdate(tier="gold",
                       buyer_profile=schemas.SponsorBuyerProfile(
                           target_role="ML engineer", industry="ml-platform")),
        db, user,
    )
    assert out.tier == "gold"
    assert out.buyer_profile.target_role == "ML engineer"
    assert out.name == "Cohere"  # name unchanged


def test_update_sponsor_wipes_cached_matches(db, user_and_event, monkeypatch):
    from backend.routes.matching import (
        create_sponsor, update_sponsor, match as match_route,
        SponsorCreate, SponsorUpdate,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    _make_prospect(db, ev, name="A", role="Senior ML platform engineer",
                   works_on="ml-platform")
    _make_prospect(db, ev, name="B", side="Hires",
                   offers="hires", seeks="ml infra", works_on="ml-platform")
    sponsor = create_sponsor(
        ev.id, SponsorCreate(
            name="Cohere",
            buyer_profile=schemas.SponsorBuyerProfile(
                target_role="ML platform engineer", seniority="Senior",
                industry="ml-platform"),
        ),
        db, user,
    )
    db.refresh(ev)
    match_route(ev.id, db=db, user=user)
    assert db.query(models.SponsorMatch).count() > 0
    # PATCH should clear the cached matches
    update_sponsor(
        ev.id, sponsor.id,
        SponsorUpdate(buyer_profile=schemas.SponsorBuyerProfile(
            target_role="marketer", industry="brand")),
        db, user,
    )
    assert db.query(models.SponsorMatch).count() == 0


def test_delete_sponsor_cascades_matches(db, user_and_event, monkeypatch):
    from backend.routes.matching import (
        create_sponsor, delete_sponsor, match as match_route, SponsorCreate,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    user, ev = user_and_event
    _make_prospect(db, ev, name="A", role="Senior ML platform engineer",
                   works_on="ml-platform")
    _make_prospect(db, ev, name="B", side="Hires",
                   offers="hires", seeks="ml infra", works_on="ml-platform")
    sponsor = create_sponsor(
        ev.id, SponsorCreate(
            name="Cohere",
            buyer_profile=schemas.SponsorBuyerProfile(
                target_role="ML platform engineer", seniority="Senior",
                industry="ml-platform"),
        ),
        db, user,
    )
    db.refresh(ev)
    match_route(ev.id, db=db, user=user)
    assert db.query(models.SponsorMatch).count() > 0
    delete_sponsor(ev.id, sponsor.id, db, user)
    assert db.query(models.SponsorMatch).count() == 0
    assert db.query(models.Sponsor).count() == 0


def test_cannot_touch_another_users_sponsor(db, user_and_event):
    """The owned-event check covers sponsor access too : a different
    user can't read / patch / delete a sponsor on someone else's event."""
    from backend.routes.matching import (
        create_sponsor, update_sponsor, delete_sponsor, list_sponsors,
        SponsorCreate, SponsorUpdate,
    )
    user, ev = user_and_event
    sponsor = create_sponsor(
        ev.id, SponsorCreate(name="Cohere"), db, user,
    )
    intruder = models.User(name="Mallory", unipile_account_id=None)
    db.add(intruder); db.commit()
    # list / patch / delete should all 404 via get_owned_event
    with pytest.raises(Exception) as exc:
        list_sponsors(ev.id, db, intruder)
    assert getattr(exc.value, "status_code", 0) == 404
    with pytest.raises(Exception):
        update_sponsor(ev.id, sponsor.id, SponsorUpdate(tier="x"), db, intruder)
    with pytest.raises(Exception):
        delete_sponsor(ev.id, sponsor.id, db, intruder)
