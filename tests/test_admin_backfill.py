"""
Tests for POST /admin/backfill-contact-links : link every Prospect with
contact_id NULL to its durable Contact via relationships.link_contact, owned
by the prospect's event's user. Exercises the route function directly against
an in-memory session (the test_followups.py pattern) -- no HTTP, no network.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes.admin import backfill_contact_links


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    u = models.User(name="Host", email="h@x.com")
    db.add(u); db.commit(); db.refresh(u)
    owned = models.Event(user_id=u.id, kind="in_person", label="Mixer", city="SF")
    orphan = models.Event(user_id=None, kind="in_person", label="Old", city="SF")
    db.add_all([owned, orphan]); db.commit()

    linkable = models.Prospect(
        event_id=owned.id, identity="p1", name="Sarah Chen",
        linkedin_url="https://www.linkedin.com/in/sarah-chen")
    no_identity = models.Prospect(
        event_id=owned.id, identity="p2", name="Mystery Person")
    ownerless = models.Prospect(
        event_id=orphan.id, identity="p3", name="Lost Soul",
        linkedin_url="https://www.linkedin.com/in/lost-soul")
    db.add_all([linkable, no_identity, ownerless]); db.commit()
    return u, linkable, no_identity, ownerless


def test_backfill_links_skips_and_fails_correctly(db):
    u, linkable, no_identity, ownerless = _seed(db)
    res = backfill_contact_links(db=db, _=None)
    assert res == {"linked": 1, "skipped": 1, "failed": 1}

    db.refresh(linkable)
    assert linkable.contact_id is not None
    ct = db.get(models.Contact, linkable.contact_id)
    assert ct.user_id == u.id
    assert ct.primary_identity_key.startswith("li:")

    db.refresh(no_identity); db.refresh(ownerless)
    assert no_identity.contact_id is None   # no strong identity -> failed
    assert ownerless.contact_id is None     # no owning user -> skipped


def test_backfill_is_idempotent_and_dedupes_contacts(db):
    u, linkable, _, _ = _seed(db)
    # A second prospect for the SAME person under the same owner must link to
    # the SAME Contact, not mint a duplicate.
    ev = db.query(models.Event).filter(models.Event.user_id == u.id).first()
    twin = models.Prospect(
        event_id=ev.id, identity="p4", name="Sarah Chen",
        linkedin_url="https://www.linkedin.com/in/sarah-chen")
    db.add(twin); db.commit()

    first = backfill_contact_links(db=db, _=None)
    assert first["linked"] == 2
    second = backfill_contact_links(db=db, _=None)  # nothing left with NULL contact_id but identity
    assert second["linked"] == 0

    db.refresh(linkable); db.refresh(twin)
    assert linkable.contact_id == twin.contact_id
    assert (db.query(models.Contact)
            .filter(models.Contact.user_id == u.id).count()) == 1
