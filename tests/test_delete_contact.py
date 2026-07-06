"""DELETE /relationships/contacts/{id}: remove a person from the book.

Owner-scoped (404 otherwise); FK cascade drops the person's children; linked
Prospect rows are UNLINKED (per-event history preserved), not deleted.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base, enable_sqlite_fk_pragma
from backend.routes.relationships import delete_contact


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    enable_sqlite_fk_pragma(engine)   # so ON DELETE CASCADE actually fires
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _user(db, email="h@x.com"):
    u = models.User(email=email, name="H")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _contact(db, u, key="li:x", name="X"):
    c = models.Contact(user_id=u.id, primary_identity_key=key, name=name)
    db.add(c); db.commit(); db.refresh(c)
    return c


def test_delete_cascades_children_and_unlinks_prospects(db):
    u = _user(db)
    c = _contact(db, u)
    db.add(models.ContactFact(user_id=u.id, contact_id=c.id, key="k", value="v",
                              source="test"))
    ev = models.Event(user_id=u.id, role="x", seniority="Staff+", co_stage="Seed",
                      headcount=10, format="Dinner", city="SF", goal="g",
                      budget=1, threshold=50)
    db.add(ev); db.commit(); db.refresh(ev)
    p = models.Prospect(event_id=ev.id, contact_id=c.id, identity="x", name="X",
                        role="x", company="x", seniority="Staff+", side="Builds",
                        works_on="x", offers="", seeks="", sources="linkedin",
                        fit_score=80, status="surfaced")
    db.add(p); db.commit(); db.refresh(p)

    res = delete_contact(c.id, db=db, user=u)

    assert res["ok"] and res["deleted_contact_id"] == c.id
    assert db.get(models.Contact, c.id) is None
    assert db.query(models.ContactFact).filter_by(contact_id=c.id).count() == 0  # cascaded
    db.refresh(p)
    assert p.contact_id is None            # prospect survived, just unlinked
    assert db.get(models.Prospect, p.id) is not None


def test_delete_404_for_another_users_contact(db):
    u1 = _user(db, "a@x.com")
    u2 = _user(db, "b@x.com")
    c = _contact(db, u1)
    with pytest.raises(HTTPException) as ei:
        delete_contact(c.id, db=db, user=u2)
    assert ei.value.status_code == 404
    assert db.get(models.Contact, c.id) is not None   # untouched


def test_delete_404_for_missing_contact(db):
    u = _user(db)
    with pytest.raises(HTTPException) as ei:
        delete_contact(999999, db=db, user=u)
    assert ei.value.status_code == 404
