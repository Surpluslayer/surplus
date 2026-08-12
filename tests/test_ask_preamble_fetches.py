"""tests/test_ask_preamble_fetches.py : the ask must load the roster ONCE.

_load_book builds the book from the Contact spine, which means it already
fetched every contact. ask_stream then needs the same ORM rows for enrichment
and id mapping, and used to fetch them again -- so a real book was
materialised twice on every ask, and on a warm book cache the second fetch
was pure waste because _load_book never touched the database at all.

This is NOT an N+1: the book build is already batched (8 queries for 650
contacts, measured). It is one redundant bulk fetch, worth removing because
the rows are large and the trip is over a network.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend import models
from backend.demo import cohort


@pytest.fixture
def book_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    cohort.generate(db, n_lawyers=1, days=20, cohort_id="preamble",
                    contacts_per_lawyer=(40, 40))
    db.commit()
    user = db.execute(select(models.User).where(
        models.User.email == "demo-lawyer-000@example.com")).scalar_one()
    try:
        yield db, user, engine
    finally:
        db.close()


def _count_contact_selects(engine, fn):
    seen = []

    @event.listens_for(engine, "before_cursor_execute")
    def _c(conn, cur, statement, params, context, executemany):
        seen.append(statement)

    try:
        out = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _c)
    return out, sum(1 for s in seen if "FROM contacts" in s.replace("\n", " "))


def test_passing_the_roster_avoids_a_second_full_fetch(book_db):
    from backend.routes.book import _load_book, _BOOK_CACHE
    from backend.agents.relationship.spine import relationships as rel_agent

    db, user, engine = book_db

    _BOOK_CACHE.clear()
    _, two = _count_contact_selects(engine, lambda: (
        _load_book(db, user), rel_agent.list_contacts(db, user.id)))

    def one_fetch():
        roster = rel_agent.list_contacts(db, user.id)
        return _load_book(db, user, contacts=roster), roster

    _BOOK_CACHE.clear()
    _, one = _count_contact_selects(engine, one_fetch)

    assert one < two, f"reusing the roster saved nothing ({one} vs {two} contact reads)"


def test_reusing_the_roster_produces_the_same_book(book_db):
    """A caching change is only allowed to change speed."""
    from backend.routes.book import _load_book, _BOOK_CACHE
    from backend.agents.relationship.spine import relationships as rel_agent

    db, user, _engine = book_db

    _BOOK_CACHE.clear()
    plain = _load_book(db, user)
    _BOOK_CACHE.clear()
    reused = _load_book(db, user, contacts=rel_agent.list_contacts(db, user.id))

    assert [c.get("id") for c in plain] == [c.get("id") for c in reused]
    assert [c.get("name") for c in plain] == [c.get("name") for c in reused]
    assert len(plain) == len(reused) > 0


def test_an_empty_roster_still_falls_back_correctly(book_db):
    """Passing an explicitly empty list must mean "this user has no contacts",
    not "go fetch them yourself" -- otherwise the reuse path silently
    re-introduces the fetch it exists to remove."""
    from backend.routes.book import _book_from_spine

    db, user, _engine = book_db
    assert _book_from_spine(db, user, contacts=[]) == []
