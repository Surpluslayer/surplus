"""tests/test_book_prerank_sql.py : the ask's SQL pre-rank.

The ask only ever reasons over ASK_BOOK_CAP contacts. It used to reach that
number the expensive way -- build every row in the book, then throw away the
ones past the cap -- so a 650-contact book hydrated 650 people's interaction
rows and reconstructed 650 timelines to send 80 of them to selection.

routes/book.py:_book_for_ask decides WHO first, from a last-touch aggregate,
and builds only those rows. That is only legitimate if it picks the SAME
people. These tests are the proof, in two halves:

  1. last_touch_index(db, contacts)[c.id] == contact_summary(...)["last_touch_at"]
     for every contact, across every source that can carry a timestamp.
  2. _spine_prerank(...) returns exactly the contacts _prioritized_for_ask
     picks off the fully built book -- same people, same order, ties included.

Both halves are checked against a book with a real tie group at the cap
boundary, because that is where an "equivalent" ranking normally stops being
equivalent (see test_the_equivalence_test_can_fail).
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.db import Base
from backend.agents.relationship import book as book_agent
from backend.agents.relationship.spine import relationships as rel_agent
from backend.routes import book as routes_book


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        routes_book._BOOK_CACHE.clear()


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _ago(days: float) -> datetime:
    """Naive UTC, which is how these columns are actually stored."""
    return (NOW - timedelta(days=days)).replace(tzinfo=None)


def _user(db, email="prerank@example.com"):
    u = models.User(email=email, name="Host")
    db.add(u)
    db.commit()
    return u


def _contact(db, user, name):
    c = models.Contact(user_id=user.id, name=name,
                       primary_identity_key=f"{user.id}:{name}")
    db.add(c)
    db.commit()
    return c


def _interaction(db, user, *, contact=None, prospect=None, days=1,
                 source_type="message", meta=None):
    ri = models.RelationshipInteraction(
        actor_user_id=user.id,
        contact_id=(contact.id if contact is not None else None),
        prospect_id=(prospect.id if prospect is not None else None),
        occurred_at=_ago(days), source_type=source_type,
        interaction_type="message", title="Message",
        meta_json=(json.dumps(meta) if meta else None))
    db.add(ri)
    db.commit()
    return ri


def _prospect(db, user, contact, *, captured_days=None):
    ev = models.Event(user_id=user.id, kind="conference", label="NYC Tech Week",
                      city="NYC", event_name="NYC Tech Week")
    db.add(ev)
    db.commit()
    p = models.Prospect(event_id=ev.id, name=contact.name, contact_id=contact.id,
                        identity=f"p:{contact.id}",
                        captured_at=(_ago(captured_days)
                                     if captured_days is not None else None),
                        source="scan")
    db.add(p)
    db.commit()
    return p


def _full_book(db, user, contacts):
    """The book exactly as _book_from_spine builds it, at a pinned `now`."""
    inter = rel_agent.prefetch_interactions_by_prospect(db, contacts)
    upd = rel_agent.prefetch_activity_updates_by_contact(db, contacts)
    by_contact = rel_agent.prefetch_interactions_by_contact(db, contacts)
    return routes_book._book_from_spine_contacts(db, user, contacts, inter, upd,
                                                 by_contact)


# ── half 1 : the index equals what the built book would have computed ───────

def test_last_touch_index_matches_contact_summary_across_every_source(db):
    """The equivalence the whole pre-rank rests on, source by source.

    build_timeline's newest non-activity_update item can only come from an
    interaction (tied to the contact or to a linked prospect), the capture
    stamp, or an outreach state transition. Each row below isolates one of
    those, plus the cases that must NOT count: a passive activity_update, a
    conversion (no timestamp column at all), and a contact with no history.
    """
    user = _user(db)

    own = _contact(db, user, "Own interaction")
    _interaction(db, user, contact=own, days=10)

    via_prospect = _contact(db, user, "Prospect interaction")
    p1 = _prospect(db, user, via_prospect, captured_days=400)
    _interaction(db, user, prospect=p1, days=7)

    capture_only = _contact(db, user, "Capture only")
    _prospect(db, user, capture_only, captured_days=120)

    outreach = _contact(db, user, "Outreach only")
    p2 = _prospect(db, user, outreach, captured_days=200)
    db.add(models.OutreachLog(prospect_id=p2.id, channel="linkedin",
                              state="sent", ts=_ago(30)))
    db.add(models.OutreachLog(prospect_id=p2.id, channel="linkedin",
                              state="replied", ts=_ago(15)))
    db.commit()

    # A conversion carries no timestamp, so build_timeline sorts it to the end
    # with occurred_at=None and the touched filter drops it. If last_touch_index
    # ever started counting it, this contact's answer would stop being 90.
    converted = _contact(db, user, "Converted")
    p3 = _prospect(db, user, converted, captured_days=90)
    db.add(models.Conversion(prospect_id=p3.id, goal="clients", tier="a",
                             state="won", label="Retained", detail="", value=0))
    db.commit()

    # An observation about them is not a touch: it must not reset the clock.
    watched = _contact(db, user, "Only watched")
    _interaction(db, user, contact=watched, days=2,
                 source_type="activity_update", meta={"draft": "Hi"})
    _interaction(db, user, contact=watched, days=60)

    never = _contact(db, user, "Never touched")

    contacts = rel_agent.list_contacts(db, user.id)
    index = rel_agent.last_touch_index(db, contacts)

    inter = rel_agent.prefetch_interactions_by_prospect(db, contacts)
    by_contact = rel_agent.prefetch_interactions_by_contact(db, contacts)
    for c in contacts:
        expected = rel_agent.contact_summary(
            db, c, inter, [], interactions_by_contact=by_contact)["last_touch_at"]
        assert index.get(c.id) == expected, (
            f"{c.name}: index says {index.get(c.id)}, the built book says {expected}")

    # And the specific answers, so a bug that broke BOTH sides identically
    # (e.g. dropping the prospect sources from each) still fails here.
    by_name = {c.name: index.get(c.id) for c in contacts}
    assert by_name["Own interaction"] == _ago(10).replace(tzinfo=timezone.utc)
    assert by_name["Prospect interaction"] == _ago(7).replace(tzinfo=timezone.utc)
    assert by_name["Capture only"] == _ago(120).replace(tzinfo=timezone.utc)
    assert by_name["Outreach only"] == _ago(15).replace(tzinfo=timezone.utc)
    assert by_name["Converted"] == _ago(90).replace(tzinfo=timezone.utc)
    assert by_name["Only watched"] == _ago(60).replace(tzinfo=timezone.utc), \
        "an activity_update was counted as a touch"
    assert never.id not in by_name.values() and by_name["Never touched"] is None


def test_days_since_is_derived_identically_on_both_paths(db):
    """The pre-rank scores days_since from the index; the book row computes it
    from the same timestamp. They call the same helper, and this pins that they
    still agree at the boundaries (None, 0, and a fractional day)."""
    user = _user(db)
    for name, days in (("today", 0.2), ("yesterday", 1.4), ("old", 365.9)):
        c = _contact(db, user, name)
        _interaction(db, user, contact=c, days=days)
    _contact(db, user, "never")

    contacts = rel_agent.list_contacts(db, user.id)
    index = rel_agent.last_touch_index(db, contacts)
    book = _full_book(db, user, contacts)
    now = datetime.now(timezone.utc)
    for c, row in zip(contacts, book):
        assert row["days_since"] == routes_book._days_since(index.get(c.id), now)


# ── half 2 : the ranking is the same ranking ────────────────────────────────

def _spread_book(db, user, n=200):
    """A roster whose days_since spans the whole priority curve, with a large
    deliberate tie group: everything quiet 51+ days scores the capped 100, so
    most of this book ties and the cap boundary lands inside the tie."""
    rng = random.Random(7)
    for i in range(n):
        c = _contact(db, user, f"C{i:03d}")
        if i % 17 == 0:
            continue                       # never touched -> days_since None
        _interaction(db, user, contact=c, days=rng.choice(
            [0, 3, 12, 23, 24, 40, 50, 51, 90, 200, 900]))
    return rel_agent.list_contacts(db, user.id)


@pytest.mark.parametrize("query", [
    "who has gone quiet",          # cadence: ranks within the needs-outreach pool
    "who should I congratulate",   # not cadence: ranks the whole roster
])
def test_prerank_picks_exactly_what_the_built_book_would_have(db, query):
    user = _user(db)
    contacts = _spread_book(db, user)
    cap = 80
    assert len(contacts) > cap, "the pre-rank is a no-op at or under the cap"

    book = _full_book(db, user, contacts)
    expected = [c["id"] for c in
                book_agent._prioritized_for_ask(book, query, cap)]

    index = rel_agent.last_touch_index(db, contacts)
    got = [str(c.id) for c in
           routes_book._spine_prerank(contacts, index, query, cap)]

    assert got == expected, "the SQL pre-rank picked a different top-{}".format(cap)


def test_the_equivalence_test_can_fail(db):
    """Teeth check: the obvious 'just order by oldest touch' pre-rank is NOT
    equivalent, and the assertion above is what catches it.

    Priority saturates at 100 for anyone quiet 51+ days, so a real book has a
    big tie group at the cap boundary. Python's stable sort breaks those ties
    by roster order; ordering by last_touch breaks them by date. Same idea,
    different 80 people -- which is exactly why this ships as a shared-code
    substitution rather than an ORDER BY.
    """
    user = _user(db)
    contacts = _spread_book(db, user)
    cap = 80
    book = _full_book(db, user, contacts)
    expected = [c["id"] for c in
                book_agent._prioritized_for_ask(book, "who has gone quiet", cap)]

    index = rel_agent.last_touch_index(db, contacts)
    naive = sorted(contacts,
                   key=lambda c: index.get(c.id) or datetime.min.replace(
                       tzinfo=timezone.utc))
    assert [str(c.id) for c in naive[:cap]] != expected


def test_prerank_is_a_noop_at_or_under_the_cap(db):
    user = _user(db)
    contacts = _spread_book(db, user, n=30)
    index = rel_agent.last_touch_index(db, contacts)
    assert routes_book._spine_prerank(contacts, index, "anything", 80) is contacts


def test_empty_index_degrades_to_roster_order_not_a_crash(db):
    """last_touch_index returns {} on a read error. Everyone then scores as
    never-touched, so the pre-rank falls back to roster order rather than
    sinking the ask."""
    user = _user(db)
    contacts = _spread_book(db, user, n=120)
    got = routes_book._spine_prerank(contacts, {}, "who has gone quiet", 80)
    assert [c.id for c in got] == [c.id for c in contacts[:80]]


# ── the shape the shortcut depends on ───────────────────────────────────────

def test_spine_rows_still_carry_the_cadence_shape_the_prerank_assumes(db):
    """_spine_prerank scores a synthetic row instead of a real one, which is
    sound only because every spine row carries these exact cadence fields. If
    the book ever starts varying tier/cadence/review_due per contact, the
    shortcut silently starts ranking a shape the book no longer has -- so pin
    it here, at the row build."""
    user = _user(db)
    c = _contact(db, user, "Anyone")
    _interaction(db, user, contact=c, days=5)
    row = _full_book(db, user, rel_agent.list_contacts(db, user.id))[0]
    assert row["tier"] == routes_book._SPINE_TIER
    assert row["cadence_days"] == routes_book._SPINE_CADENCE_DAYS
    assert row["review_due"] == routes_book._SPINE_REVIEW_DUE


# ── the wiring : which path a real ask takes ────────────────────────────────

def test_book_for_ask_takes_the_sql_path_and_builds_only_the_cap(db, monkeypatch):
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    user = _user(db)
    contacts = _spread_book(db, user)

    book, info = routes_book._book_for_ask(db, user, contacts, "who has gone quiet")

    assert info["path"] == "sql"
    assert info["roster"] == len(contacts)
    assert len(book) == 80, f"built {len(book)} rows for an 80-contact cap"
    # The rows it built are the rows selection would have kept.
    full = _full_book(db, user, contacts)
    expected = [c["id"] for c in
                book_agent._prioritized_for_ask(full, "who has gone quiet", 80)]
    assert [c["id"] for c in book] == expected


def test_book_for_ask_does_not_poison_the_full_book_cache(db, monkeypatch):
    """The narrowed book must never be stored as the user's book -- the Today
    feed reads that cache and would start showing 80 of 200 contacts."""
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    user = _user(db)
    contacts = _spread_book(db, user)
    routes_book._BOOK_CACHE.clear()

    routes_book._book_for_ask(db, user, contacts, "who has gone quiet")
    assert user.id not in routes_book._BOOK_CACHE

    assert len(routes_book._load_book(db, user, contacts=contacts)) == len(contacts)


def test_a_warm_cache_skips_the_prerank_entirely(db, monkeypatch):
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    user = _user(db)
    contacts = _spread_book(db, user)
    routes_book._BOOK_CACHE.clear()
    routes_book._load_book(db, user, contacts=contacts)     # warm it

    book, info = routes_book._book_for_ask(db, user, contacts, "who has gone quiet")
    assert info["path"] == "cache"
    assert len(book) == len(contacts), "the cached full book should be reused as-is"


def test_small_books_take_the_unchanged_full_path(db, monkeypatch):
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    user = _user(db)
    contacts = _spread_book(db, user, n=20)
    book, info = routes_book._book_for_ask(db, user, contacts, "who has gone quiet")
    assert info["path"] == "full"
    assert len(book) == 20


def test_demo_users_keep_the_demo_roster(db, monkeypatch):
    """A demo book is the spine PLUS _demo_book(), whose rows have varying
    tier/cadence/review_due. The pre-rank can neither see those rows nor score
    them with the spine shape, so demo users must not take the SQL path."""
    monkeypatch.setenv("ASK_BOOK_CAP", "80")
    monkeypatch.setattr("backend.auth.is_demo_user", lambda u: True)
    user = _user(db)
    contacts = _spread_book(db, user)
    routes_book._BOOK_CACHE.clear()

    book, info = routes_book._book_for_ask(db, user, contacts, "who has gone quiet")
    assert info["path"] == "full"
    assert len(book) > len(contacts), "the demo roster was dropped"
