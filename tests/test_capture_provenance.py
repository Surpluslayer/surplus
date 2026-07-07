"""Capture provenance must survive the prospect -> contact -> agent-context
pipe: a scanned person reads as MET (with the capture note as safe color), a
sourced list entry reads as NEVER MET (so drafts can't write "great meeting
you" to a stranger), and placeholder companies get repaired from the capture
role string. In-memory SQLite, no network."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.spine import relationships as rel
from backend.agents.relationship.pipeline.agent.run import _context_brief


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


def _host(db):
    u = models.User(name="Jia", email="jia@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    return u


def _event(db, u, label):
    e = models.Event(user_id=u.id, kind="in_person", label=label, city="")
    db.add(e); db.commit()
    return e


def _prospect(db, e, **kw):
    kw.setdefault("identity", "p")
    p = models.Prospect(event_id=e.id, **kw)
    db.add(p); db.commit()
    return p


def _contact_for(db, u, p, **kw):
    c = models.Contact(user_id=u.id,
                       primary_identity_key=f"li:{p.identity}",
                       name=p.name, **kw)
    db.add(c); db.commit()
    p.contact_id = c.id
    db.commit()
    db.refresh(c)
    return c


# ── contact_summary provenance rollup ───────────────────────────────────────

def test_summary_captured_in_person(db):
    u = _host(db)
    e = _event(db, u, "Mango tango")
    p = _prospect(db, e, identity="wesley", name="Wesley Dela Cruz",
                  role="Engineer @ AutoComplete", company="Unknown",
                  source="text", note="met at the mango stand",
                  captured_at=datetime(2026, 5, 31, 22, 26,
                                       tzinfo=timezone.utc))
    c = _contact_for(db, u, p)
    s = rel.contact_summary(db, c)
    assert s["met_in_person"] is True
    assert s["origin"] == "captured"
    assert s["met_event"] == "Mango tango"
    assert s["capture_method"] == "text"
    assert s["capture_note"] == "met at the mango stand"
    assert s["met_on"] is not None


def test_summary_sourced_never_met(db):
    u = _host(db)
    e = _event(db, u, "The Table by Women in AI")
    p = _prospect(db, e, identity="claire", name="Claire Lee",
                  company="Scritch (YC W24)", sources="linkedin")
    c = _contact_for(db, u, p, company="Scritch (YC W24)")
    s = rel.contact_summary(db, c)
    assert s["met_in_person"] is False
    assert s["origin"] == "sourced"
    assert s["met_event"] is None
    # met_at (the event we KNOW them from) still present for the UI --
    # met_in_person is the honest discriminator.
    assert s["met_at"] == "The Table by Women in AI"


def test_summary_conversation_origin(db):
    u = _host(db)
    c = models.Contact(user_id=u.id, primary_identity_key="li:dm-friend",
                       name="DM Friend")
    db.add(c); db.commit()
    s = rel.contact_summary(db, c)
    assert s["origin"] == "conversation"
    assert s["met_in_person"] is False


# ── the drafting brief consumes it honestly ─────────────────────────────────

def _brief(summary, events=None):
    ctx = {"summary": summary, "events": events or [], "prior_messages": []}
    return _context_brief({"contact_id": 1, "reason": "", "angle": ""}, ctx)


def test_brief_met_in_person_is_safe_fact(db):
    u = _host(db)
    e = _event(db, u, "Founders Inc last day :(")
    p = _prospect(db, e, identity="matty", name="mattyhempstead",
                  source="scan", note="Is Australian and likes bananas",
                  captured_at=datetime(2026, 5, 31, 10, 14,
                                       tzinfo=timezone.utc))
    c = _contact_for(db, u, p)
    brief = _brief(rel.contact_summary(db, c))
    safe = " | ".join(brief["safe_facts_to_use"])
    assert "met them in person at Founders Inc last day :(" in safe
    assert "likes bananas" in safe
    assert not any("never actually met" in a
                   for a in brief["facts_to_avoid_or_treat_as_uncertain"])


def test_brief_sourced_forbids_having_met(db):
    u = _host(db)
    e = _event(db, u, "The Table by Women in AI")
    p = _prospect(db, e, identity="claire", name="Claire Lee",
                  company="Scritch (YC W24)", sources="linkedin")
    c = _contact_for(db, u, p, company="Scritch (YC W24)")
    brief = _brief(rel.contact_summary(db, c))
    avoid = " | ".join(brief["facts_to_avoid_or_treat_as_uncertain"])
    assert "never actually met" in avoid
    assert "great meeting you" in avoid
    assert any("COLD first touch" in r for r in brief["drafting_risks"])
    assert not any("met them in person" in f
                   for f in brief["safe_facts_to_use"])


# ── company hygiene ──────────────────────────────────────────────────────────

def test_prospect_company_parses_role():
    class P:
        company = "Unknown"
        role = "Engineer @ AutoComplete"
    assert rel.prospect_company(P()) == "AutoComplete"
    P.role = "CTO at Scritch"
    assert rel.prospect_company(P()) == "Scritch"
    P.role = "Just a title"
    assert rel.prospect_company(P()) is None
    P.company = "Tenang AI"
    assert rel.prospect_company(P()) == "Tenang AI"


def test_link_contact_never_stores_placeholder(db):
    u = _host(db)
    e = _event(db, u, "Mango tango")
    p = _prospect(db, e, identity="wesley", name="Wesley Dela Cruz",
                  role="Engineer @ AutoComplete", company="Unknown",
                  linkedin_url="https://linkedin.com/in/wesley-dela-cruz")
    c = rel.link_contact(db, p, u.id)
    assert c is not None
    assert c.company == "AutoComplete"     # parsed from role, not "Unknown"


def test_backfill_contact_companies(db):
    u = _host(db)
    e = _event(db, u, "Mango tango")
    p = _prospect(db, e, identity="wesley", name="Wesley Dela Cruz",
                  role="Engineer @ AutoComplete", company="Unknown")
    c = _contact_for(db, u, p, company="Unknown")
    dry = rel.backfill_contact_companies(db, user_id=u.id, dry_run=True)
    assert dry["fixed"] == 1
    db.refresh(c)
    assert c.company == "Unknown"          # dry run does not write
    wet = rel.backfill_contact_companies(db, user_id=u.id, dry_run=False)
    assert wet["fixed"] == 1
    db.refresh(c)
    assert c.company == "AutoComplete"
