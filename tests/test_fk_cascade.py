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
from backend.routes.admin import (
    CleanupEmailContactsBody,
    MergeUsersBody,
    cleanup_email_contacts,
    merge_users,
)
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


# ── H1 : admin.merge_users ────────────────────────────────────────────────

def test_merge_users_cascades_orphan_children(db):
    """merge_users deletes the source user; its DIE-with-user children (Job,
    EmailAccount, ContactFact, ContactIdentity, ConnectedAccount,
    OutgoingMessage) cascade instead of throwing a ForeignKeyViolation, while
    MOVE children (Event/Contact) re-point to the survivor."""
    import os
    os.environ["ADMIN_TOKEN"] = "t"
    now = datetime.now(timezone.utc)
    src = models.User(email="src@example.com", name="Src")
    dst = models.User(email="dst@example.com", name="Dst")
    db.add_all([src, dst]); db.flush()

    # A MOVE child (must survive, re-pointed to dst).
    ev = models.Event(
        role="founders", seniority="Senior", co_stage="Seed", headcount=30,
        format="Mixer", city="NYC", goal="connect", budget=0, threshold=60,
        user_id=src.id)
    db.add(ev)
    contact = models.Contact(user_id=src.id, primary_identity_key="li:x",
                             name="Moved Contact")
    db.add(contact); db.flush()

    # DIE-with-user children (must cascade-delete with src).
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
    src_id, dst_id = src.id, dst.id

    out = merge_users(MergeUsersBody(from_user_id=src_id, to_user_id=dst_id,
                                     dry_run=False), db=db, _=None)
    assert out["dry_run"] is False

    # src gone; its DIE children gone.
    assert db.get(models.User, src_id) is None
    assert db.query(models.Job).filter_by(user_id=src_id).count() == 0
    assert db.query(models.EmailAccount).filter_by(user_id=src_id).count() == 0
    assert db.query(models.ConnectedAccount).filter_by(user_id=src_id).count() == 0
    assert db.query(models.ContactFact).filter_by(user_id=src_id).count() == 0
    assert db.query(models.OutgoingMessage).filter_by(user_id=src_id).count() == 0

    # MOVE children re-pointed to the survivor, not deleted.
    assert db.query(models.Event).filter_by(user_id=dst_id).count() == 1
    assert db.query(models.Contact).filter_by(user_id=dst_id).count() == 1


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


# ── H3 : admin.cleanup_email_contacts ─────────────────────────────────────

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


def test_cleanup_email_contacts_deletes_junk_with_identity(db):
    """The cleanup route deletes a junk contact and its ContactIdentity child
    without a FK violation (the identity delete is explicit; the cascade backs
    it up)."""
    import os
    os.environ["ADMIN_TOKEN"] = "t"
    u = models.User(name="Host", email="host@x.com")
    db.add(u); db.flush()
    c = _junk_email_contact(db, u)
    db.add(models.ContactIdentity(user_id=u.id, contact_id=c.id,
                                  kind="email", value="noreply@linkedin.com"))
    db.commit()
    cid = c.id

    out = cleanup_email_contacts(CleanupEmailContactsBody(dry_run=False),
                                 db=db, _=None)
    assert out["dry_run"] is False
    assert out["deleted"] == 1
    assert db.query(models.Contact).filter_by(id=cid).count() == 0
    assert db.query(models.ContactIdentity).filter_by(contact_id=cid).count() == 0


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
