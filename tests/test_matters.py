"""tests/test_matters.py : the Matter entity and its two consumers.

A matter feeds two very different things, and the tests are split along that
line because the risk is asymmetric:

  * VENUE reads case_type + amount + location. Being wrong here produces a
    wrong court in a research aid.
  * CLIENT STATUS reads status. Every category it can derive is a Rule 7.3
    EXEMPTION, and solicitation.evaluate() short-circuits to allowed for all of
    them -- so being wrong here opens the send gate for someone the rule
    protects. That is why derived_relationship_type is reported and never
    applied, and several tests below exist only to pin that it stays that way.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import matters as m, models, venue as v
from backend.db import Base
from backend.observe import logstream
from backend.solicitation import RelationshipType


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _user(db, **kw):
    u = models.User(email=kw.get("email", "l@example.com"), name="L",
                    bar_jurisdiction="NY", practice_area="corporate_ma")
    db.add(u)
    db.flush()
    return u


def _contact(db, user, name="Client", **kw):
    c = models.Contact(user_id=user.id, name=name,
                       primary_identity_key=f"{user.id}:{name}", **kw)
    db.add(c)
    db.flush()
    return c


def _matter(db, user, contact=None, **kw):
    kw.setdefault("title", "Series A")
    kw.setdefault("status", "prospective")
    mt = models.Matter(user_id=user.id,
                       contact_id=(contact.id if contact else None), **kw)
    db.add(mt)
    db.commit()
    return mt


# ─── venue: the half that finishes a forum resolution ────────────────────────

def test_a_matter_resolves_the_state_court_a_contact_alone_could_not(db):
    """The gap this entity was built to close. A contact carries a location and
    stops at the county; the case type and amount are what separate N.Y.C.
    Civil Court from Supreme Court, and they live here."""
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Kings")
    mt = _matter(db, user, c, case_type="civil_money",
                 amount_in_controversy=v.NYC_CIVIL_COURT_LIMIT + 1)

    assert v.resolve_venue(v.VenueQuery(state="NY", county="Kings")).state_court is None
    res = m.resolve(mt, c)
    assert res.state_court == "N.Y. Supreme Court"
    assert res.county == "Kings" and res.federal_district is v.FederalDistrict.EDNY


def test_matter_location_wins_per_field_not_per_object(db):
    """A New Jersey client can have a Kings County dispute. A matter that
    records only the county still inherits the client's state -- taking the
    contact's location wholesale whenever the matter is partial would
    contradict the county the user actually typed."""
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="New York")
    mt = _matter(db, user, c, location_county="Kings",
                 case_type="civil_money", amount_in_controversy=1_000)
    q = m.venue_query_for(mt, c)
    assert (q.state, q.county) == ("NY", "Kings")


def test_a_matter_with_no_location_falls_back_to_the_client(db):
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Queens")
    mt = _matter(db, user, c, case_type="probate")
    assert m.resolve(mt, c).county == "Queens"


def test_an_unknown_case_type_stops_at_the_county_rather_than_raising(db):
    """The column is free text by design (adding a case type should be a code
    change, not a migration), so an unrecognized value has to degrade to the
    same honest partial answer as not knowing the case type at all."""
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Bronx")
    mt = _matter(db, user, c, case_type="admiralty", amount_in_controversy=500)
    res = m.resolve(mt, c)
    assert res.county == "Bronx"
    assert res.state_court is None
    assert any("no case_type supplied" in u for u in res.unresolved)


def test_a_null_amount_is_not_treated_as_zero(db):
    """Zero would route to Civil Court. "Not known" must refuse to route --
    a guessed amount picks a court."""
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Kings")
    mt = _matter(db, user, c, case_type="civil_money")
    res = m.resolve(mt, c)
    assert res.state_court is None
    assert any("amount_in_controversy" in u for u in res.unresolved)


# ─── client status: the half that must not move the gate ─────────────────────

@pytest.mark.parametrize("status,expected", [
    ("open", RelationshipType.EXISTING_CLIENT),
    ("closed", RelationshipType.FORMER_CLIENT),
    ("prospective", None),
])
def test_status_maps_to_its_exemption_category(db, status, expected):
    """"prospective" mapping to None is the load-bearing row: a matter under
    DISCUSSION is what Rule 7.3 governs, not what it exempts."""
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status=status)
    rows = m.matters_for_contact(db, user.id, c.id)
    assert m.derived_relationship_type(rows) is expected


def test_an_open_matter_outranks_closed_and_prospective_ones(db):
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status="closed", title="Old")
    _matter(db, user, c, status="open", title="Live")
    _matter(db, user, c, status="prospective", title="Maybe")
    rows = m.matters_for_contact(db, user.id, c.id)
    assert m.derived_relationship_type(rows) is RelationshipType.EXISTING_CLIENT


def test_a_garbage_status_never_decides_client_status(db):
    """An import or a typo must not be able to mint an exemption."""
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status="OPEN-ish")
    rows = m.matters_for_contact(db, user.id, c.id)
    assert m.strongest(rows) is None
    assert m.derived_relationship_type(rows) is None


def test_derived_status_never_reaches_the_send_gate(db):
    """The contract in one assertion. An open matter makes this person an
    existing client in fact, and the gate STILL treats them as a prospect until
    a human says so -- because every category the derivation can return is an
    exemption, and being wrong in that direction is a bar complaint."""
    from backend.agents.relationship import solicitation_signals as sig
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status="open")
    db.commit()

    ctx = sig.resolve_context(db, user, c, "linkedin_dm")
    assert ctx.relationship_type is RelationshipType.PROSPECT, \
        "a matter row silently exempted a contact from the Rule 7.3 gate"


def test_matters_are_owner_scoped(db):
    """Scoped in the query, not filtered afterwards -- a caller must not be
    able to read another account's engagements."""
    owner = _user(db)
    other = _user(db, email="other@example.com")
    c = _contact(db, owner)
    _matter(db, owner, c, status="open")
    assert m.matters_for_contact(db, other.id, c.id) == []


def test_no_contact_means_no_matters(db):
    user = _user(db)
    assert m.matters_for_contact(db, user.id, None) == []


# ─── the Observe sections ────────────────────────────────────────────────────

def test_the_venue_section_reports_the_court_once_a_matter_exists(db):
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Kings")
    _matter(db, user, c, title="Lease dispute", case_type="landlord_tenant",
            status="open")
    joined = " | ".join(e["msg"] for e in logstream.venue_events(db, user, c))
    assert "state court: N.Y.C. Civil Court -- Housing Part" in joined
    assert "matter #" in joined and "Lease dispute" in joined
    assert "this contact has none" not in joined


def test_the_venue_section_still_says_what_is_missing_with_no_matter(db):
    user = _user(db)
    c = _contact(db, user, location_state="NY", location_county="Kings")
    joined = " | ".join(e["msg"] for e in logstream.venue_events(db, user, c))
    assert "state court: not resolved" in joined
    assert "this contact has none" in joined


def test_the_client_status_section_is_silent_without_matters(db):
    user = _user(db)
    c = _contact(db, user)
    assert list(logstream.client_status_events(db, user, c)) == []


def test_the_client_status_section_reports_the_divergence_it_will_not_resolve(db):
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status="open")
    joined = " | ".join(e["msg"] for e in logstream.client_status_events(db, user, c))
    assert "DIVERGENCE" in joined
    assert "existing_client" in joined
    assert "still treating this person as a prospect" in joined
    assert "not auto-applied on purpose" in joined


def test_the_client_status_section_goes_quiet_once_they_agree(db):
    user = _user(db)
    c = _contact(db, user, relationship_type="existing_client")
    _matter(db, user, c, status="open")
    joined = " | ".join(e["msg"] for e in logstream.client_status_events(db, user, c))
    assert "DIVERGENCE" not in joined
    assert "already treats this person as exempt" in joined


def test_a_prospective_matter_reports_no_client_relationship(db):
    user = _user(db)
    c = _contact(db, user)
    _matter(db, user, c, status="prospective")
    joined = " | ".join(e["msg"] for e in logstream.client_status_events(db, user, c))
    assert "no matter implies a client relationship" in joined
    assert "DIVERGENCE" not in joined


# ─── routes ──────────────────────────────────────────────────────────────────

def _create(db, user, **payload):
    from backend.routes.book import create_matter, MatterIn
    return create_matter(MatterIn(**payload), db=db, user=user)


def test_create_accepts_a_borough_and_stores_its_county(db):
    user = _user(db)
    out = _create(db, user, title="Dispute", borough="Brooklyn",
                  case_type="civil_money", amount_in_controversy=1_000)
    assert out["location"]["county"] == "Kings"
    assert out["venue"]["state_court"] == "N.Y.C. Civil Court -- General Civil"


@pytest.mark.parametrize("payload,field", [
    ({"title": "X", "case_type": "admiralty"}, "case_type"),
    ({"title": "X", "status": "archived"}, "status"),
    ({"title": "X", "state": "New York"}, "state"),
    ({"title": "X", "borough": "Jersey City"}, "borough"),
    ({"title": "X", "amount_in_controversy": -5}, "amount"),
])
def test_bad_input_is_rejected_not_stored(db, payload, field):
    from fastapi import HTTPException
    user = _user(db)
    with pytest.raises(HTTPException) as exc:
        _create(db, user, **payload)
    assert exc.value.status_code == 422, field
    assert db.query(models.Matter).count() == 0


def test_create_rejects_another_users_contact(db):
    from fastapi import HTTPException
    owner = _user(db)
    other = _user(db, email="other@example.com")
    c = _contact(db, owner)
    with pytest.raises(HTTPException) as exc:
        _create(db, other, title="Sneaky", contact_id=c.id)
    assert exc.value.status_code == 404
    assert db.query(models.Matter).count() == 0


def test_opening_a_matter_stamps_opened_at_once(db):
    from backend.routes.book import update_matter, MatterIn
    user = _user(db)
    mt = _matter(db, user, status="prospective")
    update_matter(mt.id, MatterIn(title=mt.title, status="open"), db=db, user=user)
    first = mt.opened_at
    assert first is not None
    update_matter(mt.id, MatterIn(title=mt.title, status="closed"), db=db, user=user)
    update_matter(mt.id, MatterIn(title=mt.title, status="open"), db=db, user=user)
    assert mt.opened_at == first, "reopening overwrote the original opened_at"


def test_update_leaves_omitted_fields_alone(db):
    from backend.routes.book import update_matter, MatterIn
    user = _user(db)
    mt = _matter(db, user, case_type="civil_money", amount_in_controversy=900,
                 location_state="NY", location_county="Kings")
    update_matter(mt.id, MatterIn(title="Renamed"), db=db, user=user)
    assert mt.title == "Renamed"
    assert (mt.case_type, mt.amount_in_controversy) == ("civil_money", 900)
    assert mt.location_county == "Kings"


def test_update_is_owner_scoped(db):
    from fastapi import HTTPException
    from backend.routes.book import update_matter, MatterIn
    owner = _user(db)
    other = _user(db, email="other@example.com")
    mt = _matter(db, owner, status="prospective")
    with pytest.raises(HTTPException) as exc:
        update_matter(mt.id, MatterIn(title="X", status="open"), db=db, user=other)
    assert exc.value.status_code == 404
    assert mt.status == "prospective"


def test_list_is_owner_scoped(db):
    from backend.routes.book import list_matters
    owner = _user(db)
    other = _user(db, email="other@example.com")
    _matter(db, owner, title="Mine")
    assert [x["title"] for x in list_matters(db=db, user=owner)["matters"]] == ["Mine"]
    assert list_matters(db=db, user=other)["matters"] == []


# ─── the relationship-type setter ────────────────────────────────────────────

def test_relationship_type_can_be_set_and_the_gate_follows(db):
    """The deliberate act the derivation refuses to perform. Without this route
    the divergence Observe reports would have been unactionable."""
    from backend.agents.relationship import solicitation_signals as sig
    from backend.routes.book import set_contact_relationship_type, RelationshipTypeIn
    user = _user(db)
    c = _contact(db, user)
    set_contact_relationship_type(
        c.id, RelationshipTypeIn(relationship_type="existing_client"),
        db=db, user=user)
    assert sig.resolve_context(db, user, c, "linkedin_dm").relationship_type is \
        RelationshipType.EXISTING_CLIENT


def test_setting_prospect_stores_nothing(db):
    """Unset and "prospect" mean the same thing to the gate; keeping one
    representation means a reader never wonders whether they differ."""
    from backend.routes.book import set_contact_relationship_type, RelationshipTypeIn
    user = _user(db)
    c = _contact(db, user, relationship_type="existing_client")
    out = set_contact_relationship_type(
        c.id, RelationshipTypeIn(relationship_type="prospect"), db=db, user=user)
    assert c.relationship_type is None
    assert out["gate_treats_as"] == "prospect"


def test_an_unknown_relationship_type_is_rejected(db):
    from fastapi import HTTPException
    from backend.routes.book import set_contact_relationship_type, RelationshipTypeIn
    user = _user(db)
    c = _contact(db, user)
    with pytest.raises(HTTPException) as exc:
        set_contact_relationship_type(
            c.id, RelationshipTypeIn(relationship_type="friend_of_a_friend"),
            db=db, user=user)
    assert exc.value.status_code == 422
    assert c.relationship_type is None


def test_the_relationship_type_route_is_owner_scoped(db):
    from fastapi import HTTPException
    from backend.routes.book import set_contact_relationship_type, RelationshipTypeIn
    owner = _user(db)
    other = _user(db, email="other@example.com")
    c = _contact(db, owner)
    with pytest.raises(HTTPException) as exc:
        set_contact_relationship_type(
            c.id, RelationshipTypeIn(relationship_type="existing_client"),
            db=db, user=other)
    assert exc.value.status_code == 404
    assert c.relationship_type is None


# ─── the migration that makes the table exist in production ──────────────────

def test_matters_has_a_migration_so_the_table_is_created_on_deploy():
    """A new model alone does NOT create its table in a deployed environment:
    create_all is skipped entirely when the stamped schema_rev already matches,
    so the revision only moves when the migrations list grows. funnel_events
    shipped without this and every write 500'd against a missing table."""
    import inspect
    import backend.db as bdb
    src = inspect.getsource(bdb.init_db)
    assert "_migrate_matters_table" in src, \
        "matters has no migration -- the table will not exist in production"
