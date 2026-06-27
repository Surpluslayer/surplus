"""
Tests for Milestone 3 : the Contact spine + stored RelationshipInteraction.

Covers the contact_id migration (additive, idempotent), lazy Contact linking
(strong-identity only, no fuzzy dedup), stored manual notes, and the unioned
derived+stored timeline. Existing event-scoped Prospect flows must keep working
with contact_id NULL.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship.spine import relationships as rel
from backend.routes import relationships as rel_route


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
    db.add(u); db.commit()
    return u


def _prospect(db, user, **kw):
    ev = models.Event(user_id=user.id, kind="in_person", label="Mixer", city="SF")
    db.add(ev); db.commit()
    p = models.Prospect(
        event_id=ev.id, identity="maya", name="Maya Rodriguez",
        role="Staff Infra", company="Lo91r",
        linkedin_url=kw.get("linkedin_url", "https://linkedin.com/in/maya"),
        status="pending", source="scan",
        captured_at=datetime.now(timezone.utc),
        note=kw.get("note"),
    )
    db.add(p); db.commit()
    return ev, p


# ── migration safety ──────────────────────────────────────────────────

def test_migration_adds_contact_id_to_legacy_table(monkeypatch):
    """A prospects table created BEFORE contact_id existed gets the column
    added by the migration, idempotently."""
    import backend.db as dbmod
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    # Simulate a legacy schema : prospects without contact_id.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE prospects (id INTEGER PRIMARY KEY, name TEXT)"))
    monkeypatch.setattr(dbmod, "ENGINE", engine)

    assert "contact_id" not in {c["name"] for c in inspect(engine).get_columns("prospects")}
    dbmod._migrate_prospect_contact_id()
    assert "contact_id" in {c["name"] for c in inspect(engine).get_columns("prospects")}
    # Idempotent : running again is a no-op (no raise, no dup column).
    dbmod._migrate_prospect_contact_id()
    cols = [c["name"] for c in inspect(engine).get_columns("prospects")]
    assert cols.count("contact_id") == 1


# ── existing Prospect flows keep working with NULL contact_id ─────────────

def test_prospect_without_contact_still_timelines(db):
    u = _user(db)
    ev, p = _prospect(db, u, note="talked KV-cache")
    assert p.contact_id is None
    tl = rel.build_timeline(p, rel.fetch_interactions(db, p))
    assert any(it["interaction_type"] == "note" for it in tl)
    s = rel.relationship_summary(p, rel.fetch_interactions(db, p))
    assert s["relationship_stage"] == "captured"


# ── lazy Contact linking ─────────────────────────────────────────────────

def test_link_contact_creates_and_links_on_strong_identity(db):
    u = _user(db)
    ev, p = _prospect(db, u)
    c = rel.link_contact(db, p, u.id)
    assert c is not None
    assert c.primary_identity_key == "li:maya"
    assert p.contact_id == c.id


def test_link_contact_is_idempotent(db):
    u = _user(db)
    ev, p = _prospect(db, u)
    c1 = rel.link_contact(db, p, u.id)
    c2 = rel.link_contact(db, p, u.id)
    assert c1.id == c2.id
    assert db.query(models.Contact).count() == 1


def test_link_contact_returns_none_without_identity(db):
    u = _user(db)
    ev, p = _prospect(db, u, linkedin_url=None)
    assert rel.link_contact(db, p, u.id) is None
    assert p.contact_id is None
    assert db.query(models.Contact).count() == 0


def test_two_prospects_same_person_share_one_contact(db):
    """Same LinkedIn slug across two events -> one Contact (event-agnostic)."""
    u = _user(db)
    _, p1 = _prospect(db, u)
    _, p2 = _prospect(db, u)  # same linkedin_url
    c1 = rel.link_contact(db, p1, u.id)
    c2 = rel.link_contact(db, p2, u.id)
    assert c1.id == c2.id
    assert db.query(models.Contact).count() == 1


# ── stored notes + unioned timeline ──────────────────────────────────────

def test_add_note_creates_interaction_and_links_contact(db):
    u = _user(db)
    ev, p = _prospect(db, u)
    ri = rel.add_note(db, p, u.id, "Wants a demo next week", visibility="team")
    assert ri.id is not None
    assert ri.source_type == "manual_note"
    assert ri.visibility == "team"
    assert ri.contact_id == p.contact_id   # linked the spine
    assert p.contact_id is not None


def test_timeline_includes_stored_note(db):
    u = _user(db)
    ev, p = _prospect(db, u, note="fun fact")
    rel.add_note(db, p, u.id, "Followed up over email")
    tl = rel.build_timeline(p, rel.fetch_interactions(db, p))
    stored = [it for it in tl if it["metadata"].get("interaction_id")]
    assert stored and stored[0]["summary"] == "Followed up over email"
    # derived note still present too
    assert any(it["summary"] == "fun fact" for it in tl)


def test_timeline_orders_derived_and_stored_chronologically(db):
    u = _user(db)
    ev, p = _prospect(db, u)
    db.add(models.OutreachLog(prospect_id=p.id, channel="linkedin",
                              state="invite_sent")); db.commit()
    rel.add_note(db, p, u.id, "latest touch")  # occurs now, after capture+invite
    tl = rel.build_timeline(p, rel.fetch_interactions(db, p))
    # The stored note is the most recent timestamped item.
    timestamped = [it for it in tl if it["occurred_at"] is not None]
    assert timestamped[-1]["summary"] == "latest touch"


def test_contact_level_note_appears_on_prospect_timeline(db):
    """A note tied to the Contact (not directly the prospect) still surfaces on
    the prospect's timeline via the contact_id union."""
    u = _user(db)
    ev, p = _prospect(db, u)
    c = rel.link_contact(db, p, u.id)
    # Note attached to the contact only, no prospect_id.
    ri = models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="manual_note",
        interaction_type="note", summary="met again at a different event",
        occurred_at=datetime.now(timezone.utc))
    db.add(ri); db.commit()
    tl = rel.build_timeline(p, rel.fetch_interactions(db, p))
    assert any(it["summary"] == "met again at a different event" for it in tl)


# ── notes route ──────────────────────────────────────────────────────────

def test_notes_route_creates_and_returns_timeline(db):
    u = _user(db)
    ev, p = _prospect(db, u)
    out = rel_route.create_note(p.id, rel_route.NoteIn(summary="ping in 2 weeks"), db, u)
    assert any(it["summary"] == "ping in 2 weeks" for it in out["timeline"])


def test_notes_route_rejects_empty_summary(db):
    from fastapi import HTTPException
    u = _user(db)
    ev, p = _prospect(db, u)
    with pytest.raises(HTTPException) as ei:
        rel_route.create_note(p.id, rel_route.NoteIn(summary="   "), db, u)
    assert ei.value.status_code == 422


def test_notes_route_blocks_unowned_prospect(db):
    from fastapi import HTTPException
    owner = _user(db, email="owner@x.com", acct="owner")
    other = _user(db, email="other@x.com", acct="other")
    ev, p = _prospect(db, owner)
    with pytest.raises(HTTPException) as ei:
        rel_route.create_note(p.id, rel_route.NoteIn(summary="sneaky"), db, other)
    assert ei.value.status_code == 404
