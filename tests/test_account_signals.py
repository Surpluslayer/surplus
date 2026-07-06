"""Account-level proactive pass (agents/relationship/account_signals.py):
rollup refresh from the sweep + the account-cooling signal. Deterministic —
no LLM, no providers."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.agents.relationship.account_signals import account_pass


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _seed(db, *, days_since_touch):
    u = models.User(name="Owner", email="o@x.com")
    db.add(u); db.commit()
    co = models.Company(canonical_name="Acme Corp")
    db.add(co); db.commit()
    contacts = []
    for i, nm in enumerate(("Jane", "Kate")):
        c = models.Contact(user_id=u.id, primary_identity_key=f"li:{nm.lower()}",
                           name=nm)
        db.add(c); db.commit()
        db.add(models.AccountMembership(user_id=u.id, contact_id=c.id,
                                        company_id=co.id, is_current=True,
                                        status="linked"))
        if days_since_touch is not None:
            db.add(models.RelationshipInteraction(
                actor_user_id=u.id, contact_id=c.id,
                source_type="manual_note", interaction_type="note",
                occurred_at=_now() - timedelta(days=days_since_touch + i)))
        contacts.append(c)
    acct = models.Account(owner_type="user", owner_id=u.id, company_id=co.id)
    db.add(acct); db.commit()
    return u, co, acct, contacts


def _cooling_rows(db):
    return (db.query(models.RelationshipInteraction)
              .filter(models.RelationshipInteraction.interaction_type ==
                      "account_cooling")
              .all())


def test_refreshes_rollups_and_emits_cooling_once(db):
    u, co, acct, (jane, kate) = _seed(db, days_since_touch=30)
    out = account_pass(db, user_id=u.id)
    assert out["rollups_changed"] == 1
    db.refresh(acct)
    assert acct.contact_count == 2
    assert acct.strength_score is not None
    assert acct.warmest_contact_id == jane.id   # 30d < kate's 31d

    rows = _cooling_rows(db)
    assert len(rows) == 1 and out["cooling_emitted"] == 1
    assert rows[0].contact_id == jane.id
    assert "Acme Corp is cooling" in rows[0].summary
    assert "2 contacts" in rows[0].summary

    # Re-run inside the window: dedup, no second nag.
    out2 = account_pass(db, user_id=u.id)
    assert out2["cooling_emitted"] == 0
    assert len(_cooling_rows(db)) == 1


def test_fresh_account_does_not_cool(db):
    u, *_ = _seed(db, days_since_touch=3)
    out = account_pass(db, user_id=u.id)
    assert out["cooling_emitted"] == 0 and not _cooling_rows(db)


def test_long_dead_account_is_dormant_not_cooling(db):
    u, *_ = _seed(db, days_since_touch=200)
    out = account_pass(db, user_id=u.id)
    assert out["cooling_emitted"] == 0 and not _cooling_rows(db)


def test_never_touched_account_is_silent(db):
    u, *_ = _seed(db, days_since_touch=None)
    out = account_pass(db, user_id=u.id)
    assert out["cooling_emitted"] == 0 and not _cooling_rows(db)
