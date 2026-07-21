"""
FK-cascade regression tests for the three delete paths that used to throw a
Postgres ForeignKeyViolation because the User/Contact child tables carried no
ON DELETE CASCADE:

  H1  admin.merge_users        : db.delete(src) with Job + EmailAccount +
                                 ContactFact + ConnectedAccount children
  H2  demo._cleanup_stale_demo_users : bulk Contact delete + user delete with
                                 ContactIdentity / ContactFact / OutgoingMessage
  H3  admin.cleanup_email_contacts : delete a contact carrying a ContactFact

The structural fix is ondelete="CASCADE" on the child FK columns (models.py),
picked up by create_all on SQLite. SQLite only ENFORCES foreign keys (and thus
cascade) when PRAGMA foreign_keys=ON, so these fixtures install the same connect
listener the app uses (backend.db.enable_sqlite_fk_pragma) : without it SQLite
silently orphans children and the test would not exercise the real behavior.

Direct-function tests against an in-memory session (the test_followups.py /
test_merge_users.py pattern) : no HTTP, no network, no backend.main import.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base, enable_sqlite_fk_pragma
from backend.routes.demo import _cleanup_stale_demo_users


@pytest.fixture
def db():
    """In-memory SQLite with foreign_keys ENFORCED, so ON DELETE CASCADE fires
    exactly as it does on Postgres."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    enable_sqlite_fk_pragma(engine)   # PRAGMA foreign_keys=ON per connection
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_fk_enforced_in_fixture(db):
    """Sanity : the fixture actually enforces FKs, else the cascade tests below
    would pass vacuously (SQLite ignoring the constraint)."""
    u = models.User(email="fk@example.com")
    db.add(u); db.flush()
    db.add(models.ContactFact(user_id=999999, contact_id=888888,
                              key="k", value="v"))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ── H1 : user delete cascades DIE-with-user children ─────────────────────

def test_user_delete_cascades_children(db):
    """Deleting a User (the privacy self-delete / operator path) cascades its
    children (Job, EmailAccount, ConnectedAccount, Contact and the contact's
    ContactFact/ContactIdentity/OutgoingMessage) instead of throwing a
    ForeignKeyViolation. (The old merge-users endpoint drove this coverage;
    the endpoint retired with the events side, the DB guarantee stays.)"""
    src = models.User(email="src@example.com", name="Src")
    db.add(src); db.flush()
    contact = models.Contact(user_id=src.id, primary_identity_key="li:x",
                             name="C")
    db.add(contact); db.flush()
    db.add(models.Job(id="job_src_1", user_id=src.id, kind="prospect",
                      status="done"))
    db.add(models.EmailAccount(user_id=src.id, provider="google",
                               address="src@gmail.com",
                               unipile_account_id="uacct_src"))
    db.add(models.ConnectedAccount(user_id=src.id, provider="google"))
    db.add(models.ContactFact(user_id=src.id, contact_id=contact.id,
                              key="based_in", value="NYC"))
    db.add(models.ContactIdentity(user_id=src.id, contact_id=contact.id,
                                  kind="linkedin", value="li:x"))
    db.add(models.OutgoingMessage(user_id=src.id, contact_id=contact.id,
                                  channel="linkedin", body="hi"))
    db.commit()
    src_id = src.id

    # Delete through the real product path (privacy self-delete): it removes
    # non-cascading children in dependency order, with the DB cascades as the
    # backstop for everything else.
    from backend import retention
    retention.delete_user_data(db, src_id, actor="self")

    assert db.get(models.User, src_id) is None
    for model in (models.Job, models.EmailAccount, models.ConnectedAccount,
                  models.ContactFact, models.OutgoingMessage,
                  models.ContactIdentity, models.Contact):
        assert db.query(model).filter_by(user_id=src_id).count() == 0


# ── H2 : demo._cleanup_stale_demo_users ───────────────────────────────────

def test_demo_cleanup_deletes_contact_with_contactfact(db, monkeypatch):
    """The bulk Contact delete (and the user delete) drop the contact's
    ContactIdentity / ContactFact / OutgoingMessage children via cascade, so
    the sweep no longer 500s on a FK violation."""
    from backend.auth import DEMO_USER_EMAIL_DOMAIN
    monkeypatch.setenv("DEMO_TTL_HOURS", "1")
    now = datetime.now(timezone.utc)
    u = models.User(
        email=f"demo-abc@{DEMO_USER_EMAIL_DOMAIN}", is_demo=True,
        last_login_at=now - timedelta(hours=48))   # stale
    db.add(u); db.flush()
    c = models.Contact(user_id=u.id, primary_identity_key="em:d",
                       name="Demo Contact", email="d@x.com")
    db.add(c); db.flush()
    db.add(models.ContactFact(user_id=u.id, contact_id=c.id,
                              key="interest", value="AI"))
    db.add(models.ContactIdentity(user_id=u.id, contact_id=c.id,
                                  kind="email", value="d@x.com"))
    db.add(models.OutgoingMessage(user_id=u.id, contact_id=c.id,
                                  channel="email", body="hi"))
    db.commit()
    uid, cid = u.id, c.id

    deleted = _cleanup_stale_demo_users(db, limit=10)
    assert deleted == 1
    assert db.get(models.User, uid) is None
    assert db.query(models.Contact).filter_by(id=cid).count() == 0
    assert db.query(models.ContactFact).filter_by(contact_id=cid).count() == 0
    assert db.query(models.ContactIdentity).filter_by(contact_id=cid).count() == 0
    assert db.query(models.OutgoingMessage).filter_by(contact_id=cid).count() == 0


# ── H3 : contact delete cascades (was: cleanup_email_contacts driver) ─────

def _junk_email_contact(db, u):
    """A deletable inbound-only email-sync junk contact (passes the cleanup
    guard: em: key, single email_sync rollup, junk local part, n_out=0)."""
    import json
    c = models.Contact(user_id=u.id, primary_identity_key="em:junk",
                       name="LinkedIn Premium", email="noreply@linkedin.com")
    db.add(c); db.flush()
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="email_sync",
        interaction_type="email_thread", direction="in",
        title="Email correspondence",
        meta_json=json.dumps({"n_in": 3, "n_out": 0, "address": c.email})))
    return c



def test_contact_delete_cascades_contactfact(db):
    """H3 belt-and-suspenders: a Contact delete cascades its ContactFact child
    (ondelete=CASCADE), so the cleanup delete site is FK-safe even for a fact
    the loop's explicit delete didn't reach. (The cleanup guard normally KEEPS
    a facted contact, so this exercises the DB-level cascade directly.)"""
    u = models.User(name="Host", email="host2@x.com")
    db.add(u); db.flush()
    c = models.Contact(user_id=u.id, primary_identity_key="em:x", name="X",
                       email="x@x.com")
    db.add(c); db.flush()
    db.add(models.ContactFact(user_id=u.id, contact_id=c.id,
                              key="topic", value="promo"))
    db.commit()
    cid = c.id

    db.delete(db.get(models.Contact, cid))
    db.commit()
    assert db.query(models.ContactFact).filter_by(contact_id=cid).count() == 0
