"""tests/test_observe_production_signal_mapping.py : practice_fit and signal
classification used to go permanently neutral/unclassified on real accounts,
because they only read backend/demo/cohort.py's meta keys (signal_category,
signal_kind) -- keys production's real detection path
(agents/relationship/updates_engine.py) never writes. Verifies the new
fallback (signal_taxonomy.production_signal_category) computes a real
category from production's actual fields: interaction_type + new_title (job
changes) or milestone_type (new_post), with rows shaped EXACTLY as
updates_engine._emit / apply_profile / apply_posts actually construct them
(see tests/test_book_update_addressability.py and the investigation this
session ran against that module for the confirmed real meta_json shapes).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import ranking_trace as rt
from backend.demo import signal_taxonomy as tax
from backend.observe import adapters


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


# ── signal_taxonomy.production_signal_category ──────────────────────────

def test_job_change_general_counsel_maps_to_gc_appointment():
    meta = {"new_title": "General Counsel", "new_company": "Acme Corp",
            "prev_company": "Old Co", "source": "brightdata"}
    assert tax.production_signal_category("job_change", meta) == "gc_appointment"


def test_job_change_founder_title_maps_to_founder_departure():
    """A job_change row's new_title containing "founder" -- production has
    no explicit "left because X" field, so a new_title of Founder/Co-founder
    is read as the departure-from-a-prior-role signal (the BD-relevant fact
    for a lawyer either way: this person just left somewhere)."""
    meta = {"new_title": "Founder", "source": "brightdata"}
    assert tax.production_signal_category("job_change", meta) == "founder_departure"


def test_job_change_generic_title_maps_to_exec_appointment():
    meta = {"new_title": "VP of Sales", "source": "brightdata"}
    assert tax.production_signal_category("job_change", meta) == "exec_appointment"


def test_job_change_missing_title_maps_to_none():
    assert tax.production_signal_category("job_change", {}) is None


def test_new_post_milestone_type_maps_to_categories():
    assert tax.production_signal_category("new_post", {"milestone_type": "acquisition"}) \
        == "acquisition_announced"
    assert tax.production_signal_category("new_post", {"milestone_type": "Series B funding"}) \
        == "funding_announcement"
    assert tax.production_signal_category("new_post", {"milestone_type": "litigation filing"}) \
        == "litigation_filed"
    assert tax.production_signal_category("new_post", {"milestone_type": "regulatory investigation"}) \
        == "regulatory_action"
    assert tax.production_signal_category("new_post", {"milestone_type": "product launch"}) \
        == "product_launch"


def test_new_post_unknown_milestone_and_no_milestone_map_to_none():
    assert tax.production_signal_category("new_post", {}) is None
    assert tax.production_signal_category("new_post", {"milestone_type": "something else"}) is None


def test_promotion_profile_update_account_cooling_have_no_mapping():
    """Honest gap, not a guess -- these production kinds have no
    fine-grained bucket in this taxonomy yet."""
    for kind in ("promotion", "profile_update", "account_cooling"):
        assert tax.production_signal_category(kind, {"new_title": "CFO"}) is None


# ── ranking_trace._practice_fit on production-shaped rows ───────────────

def _production_row(db, user, contact, interaction_type, meta):
    """A row shaped EXACTLY like updates_engine._emit writes one -- no
    signal_category/signal_kind/engaged/disposition, which is the entire
    point of this test file."""
    row = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type="activity_update",
        interaction_type=interaction_type, direction="none", title="Changed roles",
        summary="s", meta_json=json.dumps(meta),
    )
    db.add(row)
    db.commit()
    return row


def test_practice_fit_is_computed_not_neutral_on_a_production_job_change_row(db):
    user = models.User(email="prod@example.com", is_demo=False, practice_area="corporate_ma")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="p:1", name="C")
    db.add(contact)
    db.commit()
    signal = _production_row(db, user, contact, "job_change",
                             {"new_title": "General Counsel", "new_company": "X",
                              "prev_company": "Y", "source": "brightdata"})

    trace = rt.compute_trace(db, user, contact)
    pf = next(f for f in trace.factors if f.name == "practice_fit")
    assert pf.evidence["category_source"] == "production_interaction_type"
    assert pf.evidence["signal_category"] == "gc_appointment"
    # corporate_ma's seed affinity for gc_appointment is 0.85, not the 0.5
    # neutral fallback -- this factor genuinely discriminates now.
    assert pf.value == pytest.approx(0.85)


def test_practice_fit_falls_back_to_neutral_for_unmappable_production_kind(db):
    user = models.User(email="prod2@example.com", is_demo=False, practice_area="litigation")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="p:2", name="C")
    db.add(contact)
    db.commit()
    _production_row(db, user, contact, "account_cooling",
                    {"company_id": 1, "company": "X", "days_quiet": 40, "source": "account_pass"})

    trace = rt.compute_trace(db, user, contact)
    pf = next(f for f in trace.factors if f.name == "practice_fit")
    assert pf.evidence["category_source"] == "unmapped"
    assert pf.value == 0.5


def test_demo_signal_category_still_wins_when_present(db):
    """Backward compatibility: a demo-cohort row's own signal_category must
    still be used unchanged, never overridden by the production fallback."""
    user = models.User(email="demo@example.com", is_demo=True, practice_area="ip")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="d:1", name="C")
    db.add(contact)
    db.commit()
    _production_row(db, user, contact, "message",  # demo's own literal interaction_type
                    {"signal_kind": "new_post", "signal_category": "product_launch",
                     "practice_area": "ip", "engaged": True})

    trace = rt.compute_trace(db, user, contact)
    pf = next(f for f in trace.factors if f.name == "practice_fit")
    assert pf.evidence["category_source"] == "demo_signal_category"
    assert pf.evidence["signal_category"] == "product_launch"


# ── adapters.signal_trace on a production-shaped row ─────────────────────

def test_signal_trace_shows_real_kind_and_category_for_production_rows(db):
    user = models.User(email="prod3@example.com", is_demo=False, practice_area="corporate_ma")
    db.add(user)
    db.flush()
    contact = models.Contact(user_id=user.id, primary_identity_key="p:3", name="C")
    db.add(contact)
    db.commit()
    signal = _production_row(db, user, contact, "new_post",
                             {"url": "https://...", "headline": "Acme announces acquisition",
                              "milestone_type": "acquisition", "source": "brightdata"})

    trace = adapters.signal_trace(db, signal)
    d = trace.to_dict()
    kind_f = next(f for f in d["features"] if f["name"] == "signal_kind")
    cat_f = next(f for f in d["features"] if f["name"] == "signal_category")
    assert kind_f["value"] == "new_post"
    assert "production" in kind_f["evidence"]["source"]
    assert cat_f["value"] == "acquisition_announced"
    assert d["decision"] == "classified as acquisition_announced"


# ── production's real milestone vocabulary ──────────────────────────────────

def test_the_role_milestone_type_maps_instead_of_falling_through():
    """updates_engine._POST_SYSTEM returns "raise|launch|role|award|milestone".
    Only `raise` and `launch` ever matched the keyword table, so every `role`
    post logged "unclassified" -- pinning practice_fit at a neutral 0.5 and
    leaving historical_behavior with no category to compare against."""
    assert tax.production_signal_category(
        "new_post", {"milestone_type": "role", "headline": "starting as General Counsel"}
    ) == "gc_appointment"
    assert tax.production_signal_category(
        "new_post", {"milestone_type": "role", "headline": "new role at Acme"}
    ) == "exec_appointment"


def test_every_production_milestone_type_either_maps_or_is_explained():
    """No production value may fall through silently: it either lands in a
    category or produces a reason naming the value."""
    for value in sorted(tax._PRODUCTION_MILESTONE_TYPES):
        meta = {"milestone_type": value}
        got = tax.production_signal_category("new_post", meta)
        if got is None:
            reason = tax.unmapped_reason("new_post", meta)
            assert value in reason, f"{value!r} unmapped without naming itself"
        else:
            assert got in tax.ALL_SIGNAL_CATEGORIES


def test_generic_good_news_is_left_unclassified_rather_than_forced():
    """`award` and `milestone` have no honest home among the eight categories
    -- none of them means "generic good news". Forcing them into the
    nearest-looking bucket would fabricate a practice-area affinity."""
    for value in ("award", "milestone"):
        assert tax.production_signal_category("new_post", {"milestone_type": value}) is None
        reason = tax.unmapped_reason("new_post", {"milestone_type": value})
        assert "no honest bucket" in reason


def test_the_unmapped_reason_names_the_actual_values():
    """"no mapping for this interaction_type/milestone_type" is unactionable;
    it took reading _POST_SYSTEM to find that three of five types had no
    bucket. The log has to name what it saw."""
    assert "no milestone_type" in tax.unmapped_reason("new_post", {})
    assert "no new_title" in tax.unmapped_reason("job_change", {})
    assert "account_cooling" in tax.unmapped_reason("profile_update", {})
