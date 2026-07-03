"""
Tests for POST /admin/cleanup-email-contacts : delete contacts whose only
footprint is the email-sync rollup (inbound-only promotional senders minted
before the two-way filter). Route function is exercised directly against an
in-memory session (the test_admin_backfill.py pattern) -- no HTTP, no network.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes.admin import CleanupEmailContactsBody, cleanup_email_contacts


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


def _contact(db, u, key, name, email, **kw):
    c = models.Contact(user_id=u.id, primary_identity_key=key,
                       name=name, email=email, **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _rollup(db, u, c, *, n_in=3, n_out=0):
    r = models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="email_sync",
        interaction_type="email_thread", direction="in",
        title="Email correspondence",
        meta_json=json.dumps({"n_in": n_in, "n_out": n_out,
                              "address": c.email}))
    db.add(r)
    db.commit()
    return r


def _seed(db):
    u = models.User(name="Host", email="host@x.com")
    db.add(u)
    db.commit()
    db.refresh(u)

    # 1. Pure junk : em: key, inbound-only rollup, nothing else -> deletable.
    junk = _contact(db, u, "em:junk1", "LinkedIn Premium", "noreply@linkedin.com")
    _rollup(db, u, junk)

    # 2. VIP : identical footprint but starred -> survives.
    vip = _contact(db, u, "em:vip1", "Big Deal", "newsletter@bigdeal.com",
                   vip=True)
    _rollup(db, u, vip)

    # 3. Prospect-linked : the event pipeline knows this person -> survives.
    linked = _contact(db, u, "em:link1", "Sarah Chen", "updates@sarah.com")
    _rollup(db, u, linked)
    ev = models.Event(user_id=u.id, kind="in_person", label="Mixer", city="SF")
    db.add(ev)
    db.commit()
    p = models.Prospect(event_id=ev.id, identity="p1", name="Sarah Chen",
                        contact_id=linked.id)
    db.add(p)
    db.commit()

    # 4. Manual note : any interaction beyond the sync rollup -> survives.
    noted = _contact(db, u, "em:note1", "Maya R", "digest@maya.com")
    _rollup(db, u, noted)
    db.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=noted.id, source_type="manual",
        interaction_type="note", title="met at dinner"))
    db.commit()

    # 5. Real correspondence : rollup records outbound mail -> survives.
    real = _contact(db, u, "em:real1", "Leo Park", "leo@acme.com")
    _rollup(db, u, real, n_in=2, n_out=1)

    # 6. Fact memory : a ContactFact exists -> survives.
    facted = _contact(db, u, "em:fact1", "Jia W", "alerts@jia.com")
    _rollup(db, u, facted)
    db.add(models.ContactFact(user_id=u.id, contact_id=facted.id,
                              key="interest", value="climbing"))
    db.commit()

    # 7. LinkedIn-keyed contact with an email set : NOT an email-sync mint,
    #    never a candidate even with an inbound-only rollup.
    li = _contact(db, u, "li:cory-levy", "Cory Levy", "news@cory.com")
    _rollup(db, u, li)

    return u, junk, (vip, linked, noted, real, facted, li)


def test_dry_run_previews_without_touching(db):
    _u, junk, _survivors = _seed(db)
    n_contacts = db.query(models.Contact).count()
    n_inter = db.query(models.RelationshipInteraction).count()

    out = cleanup_email_contacts(CleanupEmailContactsBody(), db=db, _=None)
    assert out["dry_run"] is True
    assert out["would_delete"] == 1
    assert out["sample"] == ["LinkedIn Premium"]
    # Nothing was touched.
    assert db.query(models.Contact).count() == n_contacts
    assert db.query(models.RelationshipInteraction).count() == n_inter
    assert db.get(models.Contact, junk.id) is not None


def test_real_run_deletes_only_the_junk(db):
    _u, junk, survivors = _seed(db)
    out = cleanup_email_contacts(CleanupEmailContactsBody(dry_run=False),
                                 db=db, _=None)
    assert out["dry_run"] is False
    assert out["deleted"] == 1
    assert db.get(models.Contact, junk.id) is None
    # The junk's rollup went with it; every guarded contact survives with
    # its rollup intact.
    assert (db.query(models.RelationshipInteraction)
            .filter_by(contact_id=junk.id).count()) == 0
    for c in survivors:
        assert db.get(models.Contact, c.id) is not None
        assert (db.query(models.RelationshipInteraction)
                .filter_by(contact_id=c.id, source_type="email_sync")
                .count()) == 1


def test_unparseable_rollup_meta_is_kept(db):
    """Uncertainty is never resolved in favor of deletion."""
    u = models.User(name="Host", email="host@x.com")
    db.add(u)
    db.commit()
    db.refresh(u)
    c = _contact(db, u, "em:odd1", "Mystery", "noreply@mystery.com")
    r = _rollup(db, u, c)
    r.meta_json = "not json"
    db.commit()

    out = cleanup_email_contacts(CleanupEmailContactsBody(dry_run=False),
                                 db=db, _=None)
    assert out["deleted"] == 0
    assert out["kept"] == 1
    assert db.get(models.Contact, c.id) is not None


def test_identity_spine_touch_is_kept(db):
    """A ContactIdentity row means another system knows this person."""
    u = models.User(name="Host", email="host@x.com")
    db.add(u)
    db.commit()
    db.refresh(u)
    c = _contact(db, u, "em:id1", "Known Elsewhere", "news@known.com")
    _rollup(db, u, c)
    db.add(models.ContactIdentity(contact_id=c.id, user_id=u.id,
                                  kind="email", value="news@known.com",
                                  source="google_contacts"))
    db.commit()

    out = cleanup_email_contacts(CleanupEmailContactsBody(dry_run=False),
                                 db=db, _=None)
    assert out["deleted"] == 0
    assert db.get(models.Contact, c.id) is not None
