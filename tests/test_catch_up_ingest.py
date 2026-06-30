"""Catch Up ingest: parse + ContactFact upsert (no network)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.pipeline.context.ingest.catch_up import (
    ingest_catch_up_payload,
    parse_catch_up_html,
    parse_catch_up_payload,
    store_profile_birthdate,
    unwrap_linkedin_raw,
)
from backend.agents.relationship.spine import memory as cm


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


def _contact(db, slug="maya-rodriguez", name="Maya Rodriguez"):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u)
    db.commit()
    c = models.Contact(
        user_id=u.id,
        primary_identity_key=f"li:{slug}",
        name=name,
        linkedin_url=f"https://www.linkedin.com/in/{slug}/",
    )
    db.add(c)
    db.commit()
    return u, c


def test_unwrap_linkedin_raw_json_string():
    inner = {"elements": [{"firstName": "Maya", "publicIdentifier": "maya-rodriguez"}]}
    out = unwrap_linkedin_raw({"object": "LinkedinRawData", "data": json.dumps(inner)})
    assert out["elements"][0]["firstName"] == "Maya"


def test_parse_nested_profile_birthday():
    payload = {
        "included": [{
            "firstName": "Maya",
            "lastName": "Rodriguez",
            "publicIdentifier": "maya-rodriguez",
            "birthdate": {"month": 3, "day": 15},
        }],
    }
    events = parse_catch_up_payload(payload, kind="birthday")
    assert len(events) == 1
    assert events[0].linkedin_public_id == "maya-rodriguez"
    assert events[0].month == 3 and events[0].day == 15


def test_ingest_birthday_upserts_fact(db):
    u, c = _contact(db)
    payload = {
        "items": [{
            "miniProfile": {
                "firstName": "Maya",
                "lastName": "Rodriguez",
                "publicIdentifier": "maya-rodriguez",
                "birthdate": {"month": 3, "day": 15},
            },
        }],
    }
    stats = ingest_catch_up_payload(db, u.id, payload, kind="birthday")
    assert stats["stored"] == 1
    facts = cm.get_facts(db, c.id, key="birthday")
    assert len(facts) == 1
    assert facts[0].value == "03-15"
    assert facts[0].source == "linkedin_catch_up"
    assert facts[0].recurring is True


def test_store_profile_birthdate(db):
    u, c = _contact(db)
    ok = store_profile_birthdate(db, u.id, c, {"birthdate": {"month": 7, "day": 4}})
    assert ok is True
    facts = cm.get_facts(db, c.id, key="birthday")
    assert facts[0].source == "linkedin_profile"
    assert facts[0].value == "07-04"


def test_html_shell_parses_empty():
    assert parse_catch_up_payload("<!DOCTYPE html><html>", kind="birthday") == []


_HTML_CARD = (
    '<div role="listitem">'
    '<a href="https://www.linkedin.com/in/aidan-hyman/">'
    '<span>Aidan Hyman</span>'
    '<span>Celebrate Aidan\u2019s recent birthday on Jun 19</span>'
    'aria-label="Message Aidan Hyman: Happy belated birthday!"'
    '</div>'
)


def test_parse_catch_up_html_birthday():
    events = parse_catch_up_html(_HTML_CARD, kind="birthday")
    assert len(events) == 1
    assert events[0].name == "Aidan Hyman"
    assert events[0].linkedin_public_id == "aidan-hyman"
    assert events[0].month == 6 and events[0].day == 19


def test_ingest_html_birthday_upserts_fact(db):
    u, c = _contact(db, slug="aidan-hyman", name="Aidan Hyman")
    stats = ingest_catch_up_payload(db, u.id, _HTML_CARD, kind="birthday")
    assert stats["stored"] == 1
    facts = cm.get_facts(db, c.id, key="birthday")
    assert facts[0].value == "06-19"
    assert facts[0].source == "linkedin_catch_up"


def test_run_claimed_catch_up_sweep(db, monkeypatch):
    u, _ = _contact(db)
    u.linkedin_status = "active"
    db.commit()
    monkeypatch.setenv("CATCH_UP_INGEST_ENABLED", "1")
    monkeypatch.setenv("CATCH_UP_INGEST_GAP_SECONDS", "3600")
    monkeypatch.setattr(
        "backend.agents.relationship.updates_scheduler._claim", lambda *_a, **_k: True)
    calls = []

    def _fake_ingest(db, user, *, kind="birthday"):
        calls.append(kind)
        return {"ran": True, "stored": 1 if kind == "birthday" else 0}

    monkeypatch.setattr(
        "backend.agents.relationship.pipeline.context.ingest.catch_up.run_catch_up_ingest",
        _fake_ingest,
    )
    monkeypatch.setattr("backend.db.SessionLocal", lambda: db)
    from backend.agents.relationship.pipeline.context.ingest.catch_up import (
        run_claimed_catch_up_sweep,
        catch_up_last_tick,
    )
    tick = run_claimed_catch_up_sweep()
    assert tick["ran"] is True
    assert tick["result"]["users"] == 1
    assert tick["result"]["stored"] == 1
    assert "birthday" in calls
    assert catch_up_last_tick() == tick
