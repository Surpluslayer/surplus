"""tests/test_venue.py : geography -> candidate forum resolution.

The load-bearing property is that this module NEVER guesses. Every test below
that asserts a None is asserting the same thing from a different angle: an
unsupported state, an unlisted county, a missing case type and a missing
amount each produce a stated reason instead of a plausible-looking forum. A
venue answer nobody can check is worse than no venue answer, because it looks
like research that was done.
"""
from __future__ import annotations

import pytest

from backend import venue as v


# ─── geography ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("borough,county,district", [
    (v.Borough.MANHATTAN, "New York", v.FederalDistrict.SDNY),
    (v.Borough.BRONX, "Bronx", v.FederalDistrict.SDNY),
    (v.Borough.BROOKLYN, "Kings", v.FederalDistrict.EDNY),
    (v.Borough.QUEENS, "Queens", v.FederalDistrict.EDNY),
    (v.Borough.STATEN_ISLAND, "Richmond", v.FederalDistrict.EDNY),
])
def test_each_borough_resolves_to_its_county_and_district(borough, county, district):
    """Two of the five counties are not named after their borough (Brooklyn ->
    Kings, Staten Island -> Richmond), and the NYC boroughs split across two
    federal districts along a line that is not the obvious one: the Bronx goes
    with Manhattan to SDNY, not with the other three."""
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=borough))
    assert r.county == county
    assert r.federal_district is district
    assert r.circuit is v.Circuit.SECOND


def test_every_borough_is_mapped():
    """A new Borough member with no county would resolve to a KeyError inside
    resolve_venue rather than an unresolved reason."""
    assert set(v.BOROUGH_COUNTY) == set(v.Borough)


def test_sdny_and_edny_county_sets_do_not_overlap():
    assert not (v.SDNY_COUNTIES & v.EDNY_COUNTIES)


def test_ulster_is_not_claimed_for_sdny():
    """Recorded as an open discrepancy rather than silently resolved: 28 U.S.C.
    s112(b) enumerates eight SDNY counties and Ulster is not among them, but
    secondary summaries list it. If someone adds it, they should have the
    statute open -- and should update FEDERAL_DISTRICT_SOURCE's note, which
    this test pins alongside it."""
    assert "Ulster" not in v.SDNY_COUNTIES
    assert "Ulster" in (v.FEDERAL_DISTRICT_SOURCE.note or ""), \
        "the discrepancy stopped being recorded"


def test_an_upstate_county_is_unresolved_rather_than_guessed():
    """Albany is NDNY, whose county list this module deliberately does not
    encode. The honest output is "not in my tables", not a nearest match."""
    r = v.resolve_venue(v.VenueQuery(state="NY", county="Albany"))
    assert r.county == "Albany"
    assert r.federal_district is None
    assert any("NDNY or WDNY" in u for u in r.unresolved)


def test_another_state_resolves_to_nothing_but_a_reason():
    """Fail-closed, unlike solicitation.py's _DEFAULT_JURISDICTION -- which
    falls back to NY's rule and says so in its own comment. A venue table for
    one state must not answer for another."""
    r = v.resolve_venue(v.VenueQuery(state="CA", case_type=v.CaseType.CIVIL_MONEY,
                                     amount_in_controversy=1_000))
    assert r.county is None and r.state_court is None
    assert r.federal_district is None and r.circuit is None
    assert any("covers NY only" in u for u in r.unresolved)


# ─── state-court routing ─────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,court", [
    (1, "N.Y.C. Civil Court -- General Civil"),
    (v.NYC_CIVIL_COURT_LIMIT, "N.Y.C. Civil Court -- General Civil"),
    (v.NYC_CIVIL_COURT_LIMIT + 1, "N.Y. Supreme Court"),
])
def test_the_civil_court_ceiling_is_inclusive(amount, court):
    """At exactly the limit the matter is still within Civil Court; the
    boundary is where a stale threshold does its damage."""
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=v.Borough.MANHATTAN,
                                     case_type=v.CaseType.CIVIL_MONEY,
                                     amount_in_controversy=amount))
    assert r.state_court == court


def test_a_small_claim_over_the_ceiling_becomes_an_ordinary_civil_claim():
    """Not "no forum": a claim too big for the Small Claims Part is simply a
    general civil matter, and dropping it would read as though nowhere could
    hear it."""
    r = v.resolve_venue(v.VenueQuery(
        state="NY", borough=v.Borough.QUEENS, case_type=v.CaseType.SMALL_CLAIM,
        amount_in_controversy=v.NYC_SMALL_CLAIMS_LIMIT + 1))
    assert r.state_court == "N.Y.C. Civil Court -- General Civil"
    assert not r.unresolved


def test_landlord_tenant_routes_on_type_not_amount():
    """The categories overlap -- a housing dispute is also a money dispute --
    and the Housing Part is right regardless of the sum, so an amount large
    enough to reach Supreme Court must not pull it out of Housing."""
    r = v.resolve_venue(v.VenueQuery(
        state="NY", borough=v.Borough.BRONX, case_type=v.CaseType.LANDLORD_TENANT,
        amount_in_controversy=5_000_000))
    assert r.state_court == "N.Y.C. Civil Court -- Housing Part"


@pytest.mark.parametrize("case_type,court", [
    (v.CaseType.CRIMINAL_MISDEMEANOR, "N.Y.C. Criminal Court"),
    (v.CaseType.CRIMINAL_FELONY, "N.Y. Supreme Court (criminal term)"),
    (v.CaseType.FAMILY, "N.Y. Family Court"),
    (v.CaseType.PROBATE, "N.Y. Surrogate's Court"),
])
def test_non_monetary_case_types_route_without_an_amount(case_type, court):
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=v.Borough.BROOKLYN,
                                     case_type=case_type))
    assert r.state_court == court
    assert not r.unresolved


def test_a_money_claim_with_no_amount_says_why_it_cannot_route():
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=v.Borough.MANHATTAN,
                                     case_type=v.CaseType.CIVIL_MONEY))
    assert r.state_court is None
    assert any("amount_in_controversy" in u for u in r.unresolved)
    # Geography still resolved -- a partial answer to a partial question.
    assert r.county == "New York" and r.federal_district is v.FederalDistrict.SDNY


def test_geography_only_query_resolves_geography_and_stops():
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=v.Borough.STATEN_ISLAND))
    assert r.county == "Richmond"
    assert r.state_court is None
    assert any("no case_type supplied" in u for u in r.unresolved)


# ─── long-arm bases ──────────────────────────────────────────────────────────

def test_nyc_only_courts_are_refused_outside_the_five_boroughs():
    """Found by exercising the module, not by reading it: every case type was
    routing through the NYC table regardless of county, so an Albany small
    claim came back as "N.Y.C. Civil Court" -- a court with no jurisdiction
    over it. Upstate the equivalent is a City/Town/Village/County Court, whose
    structure this module does not encode, so the honest answer is a refusal."""
    r = v.resolve_venue(v.VenueQuery(state="NY", county="Albany",
                                     case_type=v.CaseType.SMALL_CLAIM,
                                     amount_in_controversy=3_000))
    assert r.state_court is None
    assert any("five boroughs only" in u for u in r.unresolved)


@pytest.mark.parametrize("case_type,court", [
    (v.CaseType.PROBATE, "N.Y. Surrogate's Court"),
    (v.CaseType.FAMILY, "N.Y. Family Court"),
    (v.CaseType.CRIMINAL_FELONY, "N.Y. Supreme Court (criminal term)"),
])
def test_statewide_courts_still_route_upstate(case_type, court):
    """The NYC guard must not over-correct: Supreme, Family and Surrogate's
    Court sit in every county, so refusing them outside the city would be as
    wrong as routing Civil Court into Albany."""
    r = v.resolve_venue(v.VenueQuery(state="NY", county="Albany",
                                     case_type=case_type))
    assert r.state_court == court


def test_supreme_court_still_routes_upstate_for_a_large_money_claim():
    r = v.resolve_venue(v.VenueQuery(
        state="NY", county="Albany", case_type=v.CaseType.CIVIL_MONEY,
        amount_in_controversy=v.NYC_CIVIL_COURT_LIMIT + 1))
    assert r.state_court == "N.Y. Supreme Court"


def test_nyc_counties_are_exactly_the_five_boroughs():
    assert v.NYC_COUNTIES == set(v.BOROUGH_COUNTY.values())
    assert len(v.NYC_COUNTIES) == 5


def test_bases_are_reported_only_for_facts_the_caller_asserted():
    r = v.resolve_venue(v.VenueQuery(
        state="NY", borough=v.Borough.MANHATTAN,
        transacted_business_in_forum=True, tortious_act_in_forum=True))
    assert set(r.bases) == {v.JurisdictionBasis.TRANSACTION_OF_BUSINESS,
                            v.JurisdictionBasis.TORTIOUS_ACT_IN_FORUM}


def test_no_asserted_facts_means_no_bases_not_a_default():
    r = v.resolve_venue(v.VenueQuery(state="NY", borough=v.Borough.MANHATTAN))
    assert r.bases == ()


def test_the_long_arm_citation_names_both_statutes():
    """CPLR s302 is the general long-arm; CCA s404 is the Civil Court's own and
    is narrower. Citing s404 for a general long-arm question -- as a secondary
    source prompted here -- is citing the wrong statute, so the note has to
    carry the distinction rather than pick one."""
    cite = v.LONG_ARM_SOURCE.citation
    assert "302" in cite and "404" in cite


# ─── sourcing discipline ─────────────────────────────────────────────────────

def test_nothing_in_this_module_claims_to_be_verified():
    """Mirrors test_no_shipped_rule_claims_to_be_verified for the solicitation
    table. If this fails, someone flipped a flag in code instead of after a
    legal review."""
    notes = [v.BOROUGH_COUNTY_SOURCE, v.FEDERAL_DISTRICT_SOURCE, v.CIRCUIT_SOURCE,
             v.COURT_ROUTING_SOURCE, v.LONG_ARM_SOURCE]
    assert not [n for n in notes if n.verified]


def test_a_resolution_reports_unverified_and_lists_what_it_leaned_on():
    r = v.resolve_venue(v.VenueQuery(
        state="NY", borough=v.Borough.MANHATTAN, case_type=v.CaseType.CIVIL_MONEY,
        amount_in_controversy=10_000, transacted_business_in_forum=True))
    assert r.verified is False
    assert len(r.sources) == len(set(r.sources)), "a source was listed twice"
    joined = " ".join(s.citation for s in r.sources)
    for expected in ("28 U.S.C. s112", "28 U.S.C. s41", "C.P.L.R. s302"):
        assert expected in joined, f"{expected} missing from {joined}"


def test_a_query_that_resolves_nothing_lists_no_sources():
    """`verified` is False for an empty resolution too -- via the `bool(...)`
    guard, not vacuous all()."""
    r = v.resolve_venue(v.VenueQuery(state="TX"))
    assert r.sources == () and r.verified is False


def test_to_dict_is_json_safe_and_carries_the_caveats():
    import json
    r = v.resolve_venue(v.VenueQuery(
        state="NY", borough=v.Borough.BROOKLYN, case_type=v.CaseType.PROBATE,
        domiciled_in_forum=True))
    d = r.to_dict()
    json.dumps(d)                       # must not raise on enums
    assert d["county"] == "Kings" and d["federal_district"] == "EDNY"
    assert d["bases"] == ["domicile"]
    assert d["verified"] is False and d["citations"]


# ─── the boundary with the Rule 7.3 gate ─────────────────────────────────────

def test_venue_and_the_solicitation_gate_stay_separate():
    """The two modules share the word "jurisdiction" and no fields. The
    solicitation table must never grow county/court entries: the send gate
    would not read them, and a venue table inside a compliance table makes
    both harder to review."""
    from backend import solicitation as solic
    from dataclasses import fields
    rule_fields = {f.name for f in fields(solic.JurisdictionRule)}
    assert not (rule_fields & {"county", "borough", "court", "federal_district",
                               "circuit"})
    # And venue.py must not IMPORT the send gate -- it answers a different
    # question, and coupling them would let a venue change alter a send.
    # Checked against the import statements rather than the source text: the
    # module docstring names solicitation.py on purpose, to say what this file
    # is not.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(v))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    assert not [m for m in imported if "solicitation" in m], imported
