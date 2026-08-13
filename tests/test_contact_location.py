"""tests/test_contact_location.py : the location field venue.py resolves from.

Three things are load-bearing and each has a test that fails loudly if it
stops being true:

  1. A location is never INFERRED. The columns exist so a human can say where
     someone is; a value guessed from a company address would produce a
     confidently wrong forum, which is the failure mode venue.py refuses.
  2. The capture-event backfill is the single exception, and it is weak
     evidence handled as such -- verbatim city, county only from an
     unambiguous name, and a skip on conflict.
  3. Observe's venue section stays SILENT with no location, rather than
     rendering "unresolved" on every contact forever.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.db as bdb
from backend import models, venue as v
from backend.db import Base
from backend.observe import logstream


@pytest.fixture
def engine(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    monkeypatch.setattr(bdb, "ENGINE", e)
    return e


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _user(db):
    u = models.User(email="l@example.com", name="L", bar_jurisdiction="NY",
                    practice_area="corporate_ma")
    db.add(u)
    db.flush()
    return u


def _captured(db, user, name, city):
    """A contact captured at an event in `city` -- the only real location
    signal in this schema."""
    ev = models.Event(user_id=user.id, kind="conference", label=city,
                      city=city, event_name=city)
    db.add(ev)
    db.flush()
    c = models.Contact(user_id=user.id, name=name,
                       primary_identity_key=f"{user.id}:{name}")
    db.add(c)
    db.flush()
    db.add(models.Prospect(event_id=ev.id, identity=f"p:{c.id}", name=name,
                           contact_id=c.id, source="scan"))
    db.commit()
    return c


# ─── the city-hint table ─────────────────────────────────────────────────────

def test_a_bare_city_resolves_the_state_and_leaves_the_county_open():
    """"NYC" spans five counties and the string does not say which. Resolving
    the state and stopping is the correct partial answer -- filling in the
    most populous borough would be a guess wearing an answer's clothes."""
    assert v.hint_for_city("NYC") == ("NY", None)
    assert v.hint_for_city("New York City") == ("NY", None)


@pytest.mark.parametrize("city,county", [
    ("Manhattan", "New York"), ("Brooklyn", "Kings"), ("Queens", "Queens"),
    ("Bronx", "Bronx"), ("the bronx", "Bronx"), ("Staten Island", "Richmond"),
])
def test_a_borough_name_resolves_its_county(city, county):
    assert v.hint_for_city(city) == ("NY", county)


def test_an_unrecognized_city_yields_nothing_at_all():
    """Not a state inferred from a substring. The hint table is nine entries
    for one city, not a gazetteer -- "Springfield" is in 30-odd states."""
    for city in ("Springfield", "London", "San Francisco", "New Yorkshire", ""):
        assert v.hint_for_city(city) == (None, None), city


def test_the_hint_table_only_maps_to_counties_venue_knows():
    counties = {c for _s, c in v.CITY_HINTS.values() if c}
    assert counties <= v.NYC_COUNTIES


def test_borough_and_county_round_trip():
    for borough, county in v.BOROUGH_COUNTY.items():
        assert v.borough_for_county(county) is borough
    assert v.borough_for_county("Albany") is None
    assert v.borough_for_county(None) is None


# ─── the capture-event backfill ──────────────────────────────────────────────

def test_backfill_copies_the_city_and_resolves_an_unambiguous_borough(db):
    user = _user(db)
    c = _captured(db, user, "Borough Person", "Brooklyn")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)
    assert (c.location_city, c.location_state, c.location_county) == \
        ("Brooklyn", "NY", "Kings")


def test_backfill_leaves_the_county_null_for_a_whole_city(db):
    user = _user(db)
    c = _captured(db, user, "City Person", "NYC")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)
    assert (c.location_city, c.location_state) == ("NYC", "NY")
    assert c.location_county is None, "a borough was invented for a whole-city string"


def test_backfill_copies_an_unrecognized_city_and_nothing_else(db):
    user = _user(db)
    c = _captured(db, user, "Elsewhere", "Lisbon")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)
    assert c.location_city == "Lisbon"
    assert c.location_state is None and c.location_county is None


def test_backfill_skips_a_contact_captured_in_conflicting_cities(db):
    """Two events, two cities, no way to pick -- so it picks neither. Choosing
    the first row would silently commit to whichever the query happened to
    order first."""
    user = _user(db)
    c = _captured(db, user, "Frequent Flyer", "Brooklyn")
    ev2 = models.Event(user_id=user.id, kind="conference", label="Manhattan",
                       city="Manhattan", event_name="Manhattan")
    db.add(ev2)
    db.flush()
    db.add(models.Prospect(event_id=ev2.id, identity=f"p2:{c.id}",
                           name=c.name, contact_id=c.id, source="scan"))
    db.commit()

    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)
    assert (c.location_city, c.location_state, c.location_county) == (None, None, None)


def test_backfill_never_overwrites_a_recorded_location(db):
    """The typed value wins. A capture event is weak evidence about where
    someone's matter sits; a human saying so is not."""
    user = _user(db)
    c = _captured(db, user, "Corrected", "Brooklyn")
    c.location_city, c.location_state, c.location_county = "Albany", "NY", "Albany"
    db.commit()

    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)
    assert c.location_county == "Albany", "the capture event overwrote a typed value"


def test_backfill_is_idempotent(db):
    user = _user(db)
    c = _captured(db, user, "Twice", "Queens")
    bdb._migrate_backfill_contact_location()
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    assert db.get(models.Contact, c.id).location_county == "Queens"


def test_backfill_is_a_noop_before_the_columns_exist(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE contacts"))
        conn.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY)"))
    bdb._migrate_backfill_contact_location()   # must not raise


def test_the_schema_migration_adds_all_three_columns(engine):
    from sqlalchemy import inspect
    with engine.begin() as conn:
        for col in ("location_city", "location_state", "location_county"):
            conn.execute(text(f"ALTER TABLE contacts DROP COLUMN {col}"))
    bdb._migrate_contact_location()
    cols = {c["name"] for c in inspect(engine).get_columns("contacts")}
    assert {"location_city", "location_state", "location_county"} <= cols
    bdb._migrate_contact_location()   # idempotent


# ─── the Observe section ─────────────────────────────────────────────────────

def test_the_venue_section_is_silent_without_a_location(db):
    """The reason this was not shipped with the venue module: a section that
    renders "unresolved" on every contact is a permanent empty shelf in a log
    whose contract is to show what actually ran."""
    user = _user(db)
    c = models.Contact(user_id=user.id, name="No Location",
                       primary_identity_key="x:1")
    db.add(c)
    db.commit()
    assert list(logstream.venue_events(db, user, c)) == []


def test_the_venue_section_reports_the_hierarchy_when_a_location_exists(db):
    user = _user(db)
    c = _captured(db, user, "Brooklyn Person", "Brooklyn")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)

    joined = " | ".join(e["msg"] for e in logstream.venue_events(db, user, c))
    assert "county: Kings (Brooklyn)" in joined
    assert "federal: EDNY → Second Circuit" in joined
    # The forum needs a case shape, which now lives on a Matter -- this contact
    # has none, so the section says what is missing and where it comes from.
    assert "state court: not resolved" in joined
    assert "this contact has none" in joined
    # And it must never read as a determination.
    assert "UNVERIFIED scaffolding" in joined
    assert "never a venue determination" in joined


def test_the_venue_section_says_what_a_whole_city_cannot_resolve(db):
    user = _user(db)
    c = _captured(db, user, "City Person", "NYC")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)

    joined = " | ".join(e["msg"] for e in logstream.venue_events(db, user, c))
    assert "unresolved:" in joined
    assert "EDNY" not in joined and "SDNY" not in joined, \
        "a federal district was reported for a contact with no county"
    # Nothing above asserted a forum, so there is nothing to disclaim --
    # warning that the tables are unverified would caveat a claim never made.
    assert "UNVERIFIED scaffolding" not in joined


def test_the_venue_section_does_not_leak_into_the_solicitation_gate(db):
    """Two sections, two questions. The venue block must not change what the
    Rule 7.3 trace says about the same contact."""
    user = _user(db)
    c = _captured(db, user, "Both", "Brooklyn")
    bdb._migrate_backfill_contact_location()
    db.expire_all()
    c = db.get(models.Contact, c.id)

    jur = " | ".join(e["msg"] for e in logstream.jurisdiction_events(db, user, c))
    assert "Kings" not in jur and "EDNY" not in jur


# ─── the API route ───────────────────────────────────────────────────────────

def _route(db, contact, **payload):
    from backend.routes.book import set_contact_location, ContactLocationIn
    user = db.get(models.User, contact.user_id)
    return set_contact_location(contact.id, ContactLocationIn(**payload),
                                db=db, user=user)


def test_a_borough_is_stored_as_its_county(db):
    """The convenience the API offers and the discipline it keeps: callers
    think in boroughs, statutes and court captions name counties, so the
    column holds the county and the response hands the borough back."""
    user = _user(db)
    c = models.Contact(user_id=user.id, name="P", primary_identity_key="k:1")
    db.add(c)
    db.commit()

    out = _route(db, c, borough="Staten Island")
    assert c.location_county == "Richmond"
    assert c.location_state == "NY", "borough implies the state"
    assert out["location"]["borough"] == "staten_island"


def test_an_unknown_borough_is_rejected_not_stored(db):
    from fastapi import HTTPException
    user = _user(db)
    c = models.Contact(user_id=user.id, name="P", primary_identity_key="k:1")
    db.add(c)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _route(db, c, borough="Jersey City")
    assert exc.value.status_code == 422
    assert c.location_county is None


def test_a_bad_state_code_is_rejected(db):
    from fastapi import HTTPException
    user = _user(db)
    c = models.Contact(user_id=user.id, name="P", primary_identity_key="k:1")
    db.add(c)
    db.commit()
    for bad in ("New York", "N", "12"):
        with pytest.raises(HTTPException) as exc:
            _route(db, c, state=bad)
        assert exc.value.status_code == 422
    assert c.location_state is None


def test_omitted_fields_are_left_alone_and_explicit_null_clears(db):
    """`{}` must be a no-op, not a wipe -- a PATCH-shaped body that cleared
    everything it did not mention would erase a county on a city-only edit."""
    user = _user(db)
    c = models.Contact(user_id=user.id, name="P", primary_identity_key="k:1",
                       location_city="Brooklyn", location_state="NY",
                       location_county="Kings")
    db.add(c)
    db.commit()

    _route(db, c)                              # nothing set
    assert (c.location_city, c.location_county) == ("Brooklyn", "Kings")

    _route(db, c, city="Bushwick")             # one field
    assert (c.location_city, c.location_county) == ("Bushwick", "Kings")

    _route(db, c, county=None)                 # explicit clear
    assert c.location_county is None


def test_the_route_is_owner_scoped(db):
    from fastapi import HTTPException
    owner = _user(db)
    other = models.User(email="other@example.com", name="O")
    db.add(other)
    db.flush()
    c = models.Contact(user_id=owner.id, name="P", primary_identity_key="k:1")
    db.add(c)
    db.commit()

    from backend.routes.book import set_contact_location, ContactLocationIn
    with pytest.raises(HTTPException) as exc:
        set_contact_location(c.id, ContactLocationIn(borough="brooklyn"),
                             db=db, user=other)
    assert exc.value.status_code == 404, "a non-owner learned the contact exists"
    assert c.location_county is None
