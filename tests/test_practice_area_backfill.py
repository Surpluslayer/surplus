"""tests/test_practice_area_backfill.py : practice_area stored, not inferred.

signal_taxonomy.effective_practice_area() resolves an unset practice_area to
corporate_ma at read time, so ranking behaved correctly either way. Everything
that reads the COLUMN did not: Observe's account line reported "NOT SET", and
any consumer that forgets the helper falls back to a neutral 0.5. The backfill
writes the value so the column is the single answer.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.db as bdb
from backend import models
from backend.db import Base
from backend.demo import signal_taxonomy as tax


@pytest.fixture
def engine(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    monkeypatch.setattr(bdb, "ENGINE", e)
    return e


def _users(engine):
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    try:
        return {u.email: u.practice_area for u in s.query(models.User).all()}
    finally:
        s.close()


def test_unset_practice_area_is_written_not_left_to_the_read_path(engine):
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    s.add_all([
        models.User(email="null@example.com", practice_area=None),
        models.User(email="blank@example.com", practice_area="   "),
        models.User(email="chose@example.com", practice_area="ip"),
    ])
    s.commit()
    s.close()

    bdb._migrate_backfill_practice_area()

    got = _users(engine)
    assert got["null@example.com"] == "corporate_ma"
    assert got["blank@example.com"] == "corporate_ma"
    assert got["chose@example.com"] == "ip", "overwrote a real choice"


def test_the_written_value_is_the_one_the_read_path_would_have_resolved(engine):
    """The backfill must not introduce a second source of truth: what it
    stores has to be exactly what effective_practice_area() was returning, or
    the column and the helper start disagreeing."""
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    s.add(models.User(email="a@example.com", practice_area=None))
    s.commit()
    resolved_before = tax.effective_practice_area(
        s.query(models.User).one())
    s.close()

    bdb._migrate_backfill_practice_area()
    assert _users(engine)["a@example.com"] == resolved_before


def test_backfill_is_idempotent(engine):
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    s.add(models.User(email="a@example.com", practice_area=None))
    s.commit()
    s.close()

    bdb._migrate_backfill_practice_area()
    bdb._migrate_backfill_practice_area()
    assert _users(engine)["a@example.com"] == "corporate_ma"


def test_backfill_is_a_noop_before_the_column_exists(engine):
    """Migrations run in list order on a fresh database, and this one is data,
    not schema -- it must not raise if it somehow runs against a users table
    that predates the column."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE users"))
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    bdb._migrate_backfill_practice_area()   # must not raise


def test_observe_account_line_reports_the_stored_value(engine):
    """The reason this matters on screen: Observe's account line reads the
    column directly, so before the backfill it announced the account as
    unconfigured even though ranking was already using corporate_ma."""
    from backend.observe import logstream
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    s.add(models.User(email="lawyer@example.com", practice_area=None,
                      bar_jurisdiction="NY"))
    s.commit()
    bdb._migrate_backfill_practice_area()
    s.expire_all()
    user = s.query(models.User).one()

    joined = " | ".join(e["msg"] for e in logstream.account_events(s, user))
    assert "practice_area=corporate_ma" in joined
    assert "NOT SET" not in joined
    s.close()
