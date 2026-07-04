"""Tests for LinkedIn network search (intent parsing + agent wiring)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.spine import relationships as rel
from backend.agents.relationship.pipeline.context import network_search as ns
from backend.agents.relationship.pipeline.agent import run as ragent


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    engine = create_engine("sqlite:///:memory:",
                             connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Op"), email=kw.get("email", "op@x.com"),
                    unipile_account_id=kw.get("acct", "acct1"))
    db.add(u)
    db.commit()
    return u


def _event(db, user, label="Seed Dinner"):
    ev = models.Event(user_id=user.id, kind="in_person", label=label, city="SF")
    db.add(ev)
    db.commit()
    return ev


def _contact(db, user, ev, *, name, ident, **kw):
    p = models.Prospect(
        event_id=ev.id, identity=kw.get("identity", ident), name=name,
        role=kw.get("role", "Founder"), company=kw.get("company", "Acme"),
        linkedin_url=f"https://linkedin.com/in/{ident}",
        status="pending", source="scan",
        captured_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.commit()
    return rel.link_contact(db, p, user.id)


def test_detect_network_intent():
    assert ns.detect_network_intent("who are my 2nd degree founder connections?")
    assert ns.detect_network_intent("connections of Ella Hoffmann")
    assert ns.detect_network_intent("who do I know at Stripe through my network")
    assert not ns.detect_network_intent("who should I follow up with?")


def test_match_connector_picks_longest_name():
    contacts = [
        SimpleNamespace(name="Ella Hoffmann", linkedin_public_id="ella"),
        SimpleNamespace(name="Ella", linkedin_public_id="ella-short"),
    ]
    hit = ns.match_connector("show 2nd degree through Ella Hoffmann", contacts)
    assert hit.name == "Ella Hoffmann"


def test_parse_degrees_defaults_to_second():
    assert ns.parse_degrees("founders in my network") == [2]
    assert 3 in ns.parse_degrees("3rd degree investors")


def test_extract_keywords_pulls_company():
    q = "who in my second degree connections works at cursor?"
    assert ns.extract_keywords(q) == "cursor"


def test_detect_referral_intent():
    assert ns.detect_referral_intent("who can intro me to a PM at Stripe?")
    assert ns.detect_referral_intent("warm intro to a founder")


def test_match_connector_through_phrase():
    contacts = [SimpleNamespace(name="Ella Hoffmann", id=1, linkedin_public_id="ella")]
    hit = ns.match_connector("2nd degree through Ella Hoffmann at Cursor", contacts)
    assert hit.name == "Ella Hoffmann"


def test_connector_fanout_merges_paths(db, monkeypatch):
    u = _user(db)
    ev = _event(db, u)
    ella = _contact(db, u, ev, name="Ella Hoffmann", ident="ella")
    calls = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        if kwargs.get("connections_of"):
            return [{"name": "Warm Lead", "public_identifier": "warm",
                     "headline": "GTM @ Cursor", "network_distance": "DISTANCE_2"}]
        return [{"name": "Broad Lead", "public_identifier": "broad",
                 "headline": "Building Cursor", "network_distance": "DISTANCE_2"}]

    monkeypatch.setattr(ns, "_resolve_member_id", lambda prov, c: "ACoELLA")
    res = ns.search_linkedin_network(
        u,
        "who works at cursor in my 2nd degree network?",
        [ella],
        search_fn=fake_search,
    )
    assert any(c.get("connections_of") for c in calls)
    assert any(c.get("network_distance") for c in calls)
    assert res.hits[0].via_connector == "Ella Hoffmann"
    assert any(h.via_connector == "" for h in res.hits)


def test_enrich_book_ask_replaces_book_only_answer():
    user = SimpleNamespace(unipile_account_id="acct1")
    contacts = [SimpleNamespace(name="Ella Hoffmann", id=1, linkedin_public_id="ella")]
    book_answer = {
        "answer": "No 2nd degree founders in NYC identified in your book.",
        "people": [],
    }

    def fake_search(**kwargs):
        return [{
            "name": "Alex Kim",
            "public_identifier": "alexkim",
            "headline": "Founder in NYC",
            "network_distance": "DISTANCE_2",
        }]

    out = ns.enrich_book_ask(
        user,
        "2nd degree founders in NYC",
        contacts,
        book_answer,
        search_fn=fake_search,
    )
    assert len(out["network_hits"]) == 1
    assert out["network_hits"][0]["name"] == "Alex Kim"
    assert "Alex Kim" in out["answer"]
    assert "in your book" not in out["answer"].lower()


def test_search_dedupes_roster_and_via_connector(db, monkeypatch):
    u = _user(db)
    ev = _event(db, u)
    ella = _contact(db, u, ev, name="Ella Hoffmann", ident="ella")
    rohan_c = _contact(db, u, ev, name="Rohan Patel", ident="rohan")

    calls = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        if kwargs.get("connections_of"):
            return [
                {"name": "Rohan Patel", "public_identifier": "rohan",
                 "headline": "CEO", "network_distance": "DISTANCE_2"},
                {"name": "Naythan Lee", "public_identifier": "naythan",
                 "headline": "CTO", "network_distance": "DISTANCE_2"},
            ]
        return []

    monkeypatch.setattr(
        ns, "_resolve_member_id",
        lambda prov, c: "ACoELLA" if c.id == ella.id else "",
    )
    res = ns.search_linkedin_network(
        u,
        "2nd degree connections of Ella Hoffmann",
        [ella, rohan_c],
        search_fn=fake_search,
    )
    via_calls = [c for c in calls if c.get("connections_of")]
    assert via_calls and via_calls[0].get("connections_of") == ["ACoELLA"]
    assert len(res.hits) == 1
    assert res.hits[0].name == "Naythan Lee"
    assert res.hits[0].via_connector == "Ella Hoffmann"


def test_agent_injects_network_block_into_triage(db, monkeypatch):
    u = _user(db)
    ev = _event(db, u)
    _contact(db, u, ev, name="Ella Hoffmann", ident="ella")

    fake_hit = ns.NetworkHit(
        name="Karthik Sridharan",
        headline="Founder",
        linkedin_slug="karthik",
        network_degree="2",
    )

    def fake_network(user, instruction, contacts, **kw):
        return ns.NetworkSearchResult(
            hits=[fake_hit],
            intent={"degrees": [2], "via_connector": None, "keywords": "founder"},
        )

    monkeypatch.setattr(ragent, "search_linkedin_network", fake_network)

    triage = [SimpleNamespace(
        type="tool_use", name="select_followups", id="tg",
        input={"selections": [], "closing": "Karthik is a 2nd-degree founder match."},
    )]
    recorded = []

    def create(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(stop_reason="tool_use", content=triage)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))

    res = ragent.run_relationship_agent_concurrent(
        db, u.id,
        instruction="who are 2nd degree founders in my network?",
        client=client,
    )
    assert res.error is None
    assert len(res.network_hits) == 1
    assert res.network_hits[0]["name"] == "Karthik Sridharan"
    prompt = recorded[0]["messages"][0]["content"]
    assert "NETWORK SEARCH RESULTS" in prompt
    assert "Karthik Sridharan" in prompt
    assert "Karthik" in res.summary


def test_agent_network_only_without_contacts(db, monkeypatch):
    u = _user(db)

    fake_hit = ns.NetworkHit(
        name="Alex Kim",
        headline="PM at Stripe",
        linkedin_slug="alexkim",
        network_degree="2",
    )

    monkeypatch.setattr(
        ragent, "search_linkedin_network",
        lambda user, instruction, contacts, **kw: ns.NetworkSearchResult(hits=[fake_hit]),
    )

    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(
            AssertionError("should not call triage"))),
    )
    res = ragent.run_relationship_agent_concurrent(
        db, u.id,
        instruction="2nd degree PMs at Stripe",
        client=client,
    )
    assert res.stop_reason == "network_only"
    assert "Alex Kim" in res.summary
    assert res.network_hits[0]["linkedin_slug"] == "alexkim"
