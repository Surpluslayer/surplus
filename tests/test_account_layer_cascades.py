"""Cascade-delete verification for the account layer (design doc §6
prerequisite): deleting a contact or user must clean its account-layer
children; compliance rows (walls, audit, teams) must SURVIVE their creator.
SQLite enforces the model-level ondelete= clauses here (PRAGMA foreign_keys
is enabled by the app); Postgres gets the same semantics via
_migrate_fk_cascade's action column."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    u = models.User(name="Owner", email="o@x.com")
    db.add(u); db.commit()
    co = models.Company(canonical_name="Acme")
    db.add(co); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:jane", name="Jane")
    db.add(c); db.commit()
    db.add(models.AccountMembership(user_id=u.id, contact_id=c.id,
                                    company_id=co.id, is_current=True,
                                    status="linked"))
    a = models.Account(owner_type="user", owner_id=u.id, company_id=co.id,
                       warmest_contact_id=c.id, contact_count=1)
    db.add(a)
    t = models.Team(name="T", created_by=u.id)
    db.add(t); db.commit()
    db.add(models.TeamMembership(team_id=t.id, user_id=u.id, role="admin"))
    w = models.Wall(team_id=t.id, subject_company_id=co.id, created_by=u.id)
    db.add(w)
    log = models.TeamAuditLog(team_id=t.id, actor_user_id=u.id,
                          event="wall_created")
    db.add(log); db.commit()
    return u, co, c, a, t, w, log


def test_contact_delete_cascades_membership_and_nulls_warmest(db):
    u, co, c, a, t, w, log = _seed(db)
    db.delete(c); db.commit()
    assert db.query(models.AccountMembership).count() == 0
    db.refresh(a)
    assert a.warmest_contact_id is None      # account row survives
    assert db.get(models.Account, a.id) is not None


def test_user_delete_cascades_graph_but_compliance_survives(db):
    u, co, c, a, t, w, log = _seed(db)
    # Contacts cascade from users already (pre-existing FK) — delete the
    # contact first to isolate the account-layer FKs under test.
    db.delete(c); db.commit()
    db.delete(u); db.commit()
    assert db.query(models.TeamMembership).count() == 0
    team = db.get(models.Team, t.id)
    wall = db.get(models.Wall, w.id)
    row = db.get(models.TeamAuditLog, log.id)
    assert team is not None and team.created_by is None
    assert wall is not None and wall.created_by is None
    assert row is not None and row.actor_user_id is None


def test_team_delete_cascades_walls_memberships_audit(db):
    u, co, c, a, t, w, log = _seed(db)
    db.delete(t); db.commit()
    assert db.query(models.Wall).count() == 0
    assert db.query(models.TeamMembership).count() == 0
    assert db.query(models.TeamAuditLog).count() == 0
