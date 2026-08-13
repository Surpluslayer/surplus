"""venue.py : geography -> candidate forum resolution (scaffolding, not certified).

WHAT THIS IS NOT. It is not backend/solicitation.py. That module answers "may
this lawyer SEND this outreach?" -- an attorney-advertising question under
Rule 7.3, whose `JurisdictionRule` holds channel restrictions, disclosure
labels, cooldowns and volume caps. This module answers "if a matter arose HERE,
which forums could hear it, and on what basis might a court reach this party?"
The two share the word "jurisdiction" and share no fields. They are
deliberately separate files, and JURISDICTION_RULES must never grow county or
court entries -- the send gate would not read them, and mixing a venue table
into a compliance table makes both harder to review.

WHAT IT RETURNS. Candidates, never conclusions. `resolve_venue()` maps a
location and a case shape onto the forums that COULD be implicated, plus the
statutory long-arm bases that COULD support personal jurisdiction. Which court
actually has jurisdiction over a real matter -- and whether a specific
defendant is reachable -- is a legal determination that depends on facts this
module never sees (where the conduct occurred, what was contracted for,
removal, forum-selection clauses, amount actually pleaded). A resolution here
is a research starting point.

FAIL CLOSED. Unlike solicitation.py's _DEFAULT_JURISDICTION, an unknown state
or an unmatched case type resolves to UNRESOLVED with a stated reason. There is
no "closest guess" fallback: a wrong venue delivered confidently is worse than
no venue, and this table covers exactly one state.

SOURCING. Every table below carries a SourceNote with the provision it was
modeled on and a `verified` flag, matching the discipline in solicitation.py
(`JurisdictionRule.citation` / `citation_verified`). NOTHING here is verified.
The county-to-district assignments are statutory (28 U.S.C. s112) and the
monetary thresholds are statutory and AMENDED PERIODICALLY -- New York City
Civil Court's general civil limit moved within the last few years, which is
exactly the kind of number that goes stale silently. Confirm before any of
this informs advice.

Deliberately pure, mirroring solicitation.py and redaction.py: no DB reads, no
network calls, no LLM. Callers resolve the location themselves and pass it in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class SourceNote:
    """Where a table came from, and whether anyone checked it.

    Two fields rather than one for the reason spelled out in
    solicitation.py:JurisdictionRule.citation_verified -- a citation records
    provenance, not correctness, and a surface that treats "has a citation" as
    "is verified" makes the best-documented table look the most authoritative
    when it may be the stalest."""
    citation: str
    verified: bool = False
    note: Optional[str] = None


# ─── 1. Geography ────────────────────────────────────────────────────────────

class Borough(str, Enum):
    MANHATTAN = "manhattan"
    BROOKLYN = "brooklyn"
    QUEENS = "queens"
    BRONX = "bronx"
    STATEN_ISLAND = "staten_island"


# New York City's five boroughs are coextensive with five counties, and the
# county -- not the borough -- is the name every court caption and statute
# uses. Two of the five differ from the borough name, which is the whole
# reason this mapping exists rather than a .title() call.
BOROUGH_COUNTY: dict[Borough, str] = {
    Borough.MANHATTAN: "New York",
    Borough.BROOKLYN: "Kings",
    Borough.QUEENS: "Queens",
    Borough.BRONX: "Bronx",
    Borough.STATEN_ISLAND: "Richmond",
}

BOROUGH_COUNTY_SOURCE = SourceNote(
    citation="N.Y. County Law; N.Y.C. Charter (five boroughs = five counties)",
    verified=False,
    note="Stable geography rather than a rule that gets amended, but still "
         "unverified against a primary source.",
)


COUNTY_BOROUGH: dict[str, Borough] = {c: b for b, c in BOROUGH_COUNTY.items()}


def borough_for_county(county: Optional[str]) -> Optional[Borough]:
    """The borough a New York City county IS, or None for anywhere else.
    Storage keeps counties (statutes and captions name those); display often
    wants the borough back."""
    return COUNTY_BOROUGH.get((county or "").strip())


# City strings this module is willing to turn into a state and, where the
# string names a borough, a county. DELIBERATELY TINY: it covers the one city
# this account works in, and every entry is a name that identifies its place
# unambiguously. It is NOT a general city-to-state gazetteer and must not grow
# into one -- "Springfield" is in 30-odd states, and a lookup that guesses
# wrong here produces a confident forum for the wrong half of the country.
#
# Note what the bare-city entries do NOT do: "NYC" resolves the state and
# leaves the county None, because the city spans five counties and nothing in
# the string says which. That is the correct partial answer, not a gap to fill
# with the most populous borough.
CITY_HINTS: dict[str, tuple[str, Optional[str]]] = {
    "nyc": ("NY", None),
    "new york": ("NY", None),
    "new york city": ("NY", None),
    "manhattan": ("NY", "New York"),
    "brooklyn": ("NY", "Kings"),
    "queens": ("NY", "Queens"),
    "bronx": ("NY", "Bronx"),
    "the bronx": ("NY", "Bronx"),
    "staten island": ("NY", "Richmond"),
}


def hint_for_city(city: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(state, county) for a city string this module recognizes, else
    (None, None). Never a partial guess: an unrecognized city yields nothing
    rather than a state inferred from a substring."""
    return CITY_HINTS.get((city or "").strip().lower(), (None, None))


class FederalDistrict(str, Enum):
    SDNY = "SDNY"
    EDNY = "EDNY"
    NDNY = "NDNY"
    WDNY = "WDNY"


class Circuit(str, Enum):
    SECOND = "Second Circuit"


# 28 U.S.C. s112 defines each New York district by enumerating its counties.
# Only the two districts covering New York City and its immediate surroundings
# are enumerated here; NDNY and WDNY exist in the enum so a caller can name
# them, but this table does not claim to list their counties.
#
# ULSTER IS DELIBERATELY ABSENT FROM SDNY. It is listed as an SDNY county in
# some informal summaries. s112(b) enumerates the Southern District as Bronx,
# Dutchess, New York, Orange, Putnam, Rockland, Sullivan and Westchester --
# eight counties, not including Ulster. This is exactly the sort of
# off-by-one-county error that a confident-sounding secondary source
# introduces, so the discrepancy is recorded here rather than silently
# resolved in either direction. Confirm against the statute before relying on
# any row in this table.
SDNY_COUNTIES = frozenset({
    "Bronx", "Dutchess", "New York", "Orange",
    "Putnam", "Rockland", "Sullivan", "Westchester",
})
EDNY_COUNTIES = frozenset({"Kings", "Nassau", "Queens", "Richmond", "Suffolk"})

FEDERAL_DISTRICT_SOURCE = SourceNote(
    citation="28 U.S.C. s112(b) (Southern District), s112(c) (Eastern District)",
    verified=False,
    note="Ulster County is excluded from SDNY here; some secondary sources "
         "include it. Unresolved -- see the block comment above SDNY_COUNTIES.",
)

# 28 U.S.C. s41 places New York, Connecticut and Vermont in the Second Circuit,
# so every New York district appeals to the same court. Kept as a mapping
# rather than a constant because the shape generalizes if this ever covers a
# second state.
DISTRICT_CIRCUIT: dict[FederalDistrict, Circuit] = {
    FederalDistrict.SDNY: Circuit.SECOND,
    FederalDistrict.EDNY: Circuit.SECOND,
    FederalDistrict.NDNY: Circuit.SECOND,
    FederalDistrict.WDNY: Circuit.SECOND,
}

CIRCUIT_SOURCE = SourceNote(
    citation="28 U.S.C. s41 (Second Circuit: Connecticut, New York, Vermont)",
    verified=False,
)


# ─── 2. State-court routing ──────────────────────────────────────────────────

class CaseType(str, Enum):
    CIVIL_MONEY = "civil_money"
    LANDLORD_TENANT = "landlord_tenant"
    SMALL_CLAIM = "small_claim"
    CRIMINAL_MISDEMEANOR = "criminal_misdemeanor"
    CRIMINAL_FELONY = "criminal_felony"
    FAMILY = "family"
    PROBATE = "probate"


# Monetary ceilings for the New York City Civil Court. THESE ARE THE MOST
# PERISHABLE VALUES IN THIS FILE: both are set by statute and both have been
# raised in recent memory, so a stale number here silently routes a matter to
# the wrong court. They are named constants so a reviewer can find and check
# them in one place.
NYC_CIVIL_COURT_LIMIT = 50_000
NYC_SMALL_CLAIMS_LIMIT = 10_000

COURT_ROUTING_SOURCE = SourceNote(
    citation="N.Y.C. Civil Court Act (general civil and small claims monetary "
             "jurisdiction); N.Y. Const. art. VI (Supreme, Family, Surrogate's)",
    verified=False,
    note=f"Thresholds encoded as ${NYC_CIVIL_COURT_LIMIT:,} general civil and "
         f"${NYC_SMALL_CLAIMS_LIMIT:,} small claims. Both are amended "
         f"periodically; confirm the current figures.",
)


@dataclass(frozen=True)
class VenueQuery:
    """Everything resolve_venue() needs, supplied by the caller. Nothing here
    is inferred: a module that guessed the borough from a company address
    would be making the very determination it is meant to leave to a human."""
    state: str                                   # USPS 2-letter code
    borough: Optional[Borough] = None
    county: Optional[str] = None                 # used when borough is None
    case_type: Optional[CaseType] = None
    amount_in_controversy: Optional[int] = None  # whole dollars
    # Facts a caller may already know that bear on the long-arm analysis. Each
    # one that is True adds a CANDIDATE basis, never a conclusion.
    transacted_business_in_forum: bool = False
    contracted_to_supply_in_forum: bool = False
    tortious_act_in_forum: bool = False
    owns_real_property_in_forum: bool = False
    domiciled_in_forum: bool = False


class JurisdictionBasis(str, Enum):
    """Candidate grounds for personal jurisdiction over a non-domiciliary.

    Modeled on New York's long-arm statute. NOTE THE TWO STATUTES: CPLR s302
    is the general long-arm that governs in the Supreme Court and is what most
    analyses mean by "New York's long-arm"; N.Y.C. Civil Court Act s404 is the
    Civil Court's own, and is narrower. A secondary source citing s404 for a
    general long-arm question is citing the wrong one -- which forum the claim
    sits in decides which statute applies."""
    DOMICILE = "domicile"
    TRANSACTION_OF_BUSINESS = "transaction_of_business"
    CONTRACT_TO_SUPPLY_IN_FORUM = "contract_to_supply_in_forum"
    TORTIOUS_ACT_IN_FORUM = "tortious_act_in_forum"
    REAL_PROPERTY_IN_FORUM = "real_property_in_forum"


LONG_ARM_SOURCE = SourceNote(
    citation="N.Y. C.P.L.R. s302(a) (general long-arm); N.Y.C. Civil Ct. Act "
             "s404 (Civil Court long-arm, narrower)",
    verified=False,
    note="Bases are candidates for research, not a jurisdictional holding. "
         "Due-process minimum-contacts analysis is separate and not modeled.",
)


@dataclass(frozen=True)
class VenueResolution:
    """The hierarchy, as far as the inputs supported resolving it.

    Every field is Optional because a partial answer is the honest output of a
    partial question: a query with a borough but no case type resolves the
    geography and leaves `state_court` None, rather than picking a court."""
    state: Optional[str] = None
    borough: Optional[Borough] = None
    county: Optional[str] = None
    state_court: Optional[str] = None
    federal_district: Optional[FederalDistrict] = None
    circuit: Optional[Circuit] = None
    bases: tuple[JurisdictionBasis, ...] = ()
    # Why anything above is still None. Empty means everything the query asked
    # for resolved; a caller should surface these rather than render a blank.
    unresolved: tuple[str, ...] = ()
    sources: tuple[SourceNote, ...] = field(default_factory=tuple)

    @property
    def verified(self) -> bool:
        """True only if EVERY table this resolution leaned on was verified.
        Nothing satisfies this today; it exists so a consumer can gate on it
        instead of re-deriving the answer from the notes."""
        return bool(self.sources) and all(s.verified for s in self.sources)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "borough": self.borough.value if self.borough else None,
            "county": self.county,
            "state_court": self.state_court,
            "federal_district": (self.federal_district.value
                                 if self.federal_district else None),
            "circuit": self.circuit.value if self.circuit else None,
            "bases": [b.value for b in self.bases],
            "unresolved": list(self.unresolved),
            "verified": self.verified,
            "citations": [s.citation for s in self.sources],
        }


def federal_district_for_county(county: str) -> Optional[FederalDistrict]:
    """SDNY / EDNY for a New York county, or None when this table does not
    enumerate it. None is a real answer here -- NDNY and WDNY cover most of the
    state and their county lists are deliberately not encoded, so a caller must
    not read None as "no federal court"."""
    name = (county or "").strip()
    if name in SDNY_COUNTIES:
        return FederalDistrict.SDNY
    if name in EDNY_COUNTIES:
        return FederalDistrict.EDNY
    return None


# The five counties the New York City courts cover. This matters because the
# routing table below mixes two kinds of forum: the NYC Civil and Criminal
# Courts exist ONLY in the city, while Supreme, Family and Surrogate's Court
# sit in every county in the state. Without this distinction an Albany matter
# routed to "N.Y.C. Civil Court" -- a court with no jurisdiction over it -- which
# is precisely the confident-but-wrong answer this module exists to refuse.
NYC_COUNTIES = frozenset(BOROUGH_COUNTY.values())


def _state_court(case_type: Optional[CaseType], amount: Optional[int],
                 county: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(court, unresolved_reason) for a New York matter.

    Ordered most-specific first, because the categories overlap: a landlord-
    tenant dispute is also a civil money matter, and the Housing Part is the
    right answer for it regardless of amount."""
    if case_type is None:
        return None, "no case_type supplied -- court not determined"

    in_nyc = county in NYC_COUNTIES

    def _nyc_only(court: str) -> tuple[Optional[str], Optional[str]]:
        """A New York City court, or a stated refusal outside the five
        boroughs. Upstate the equivalent is a City, Town, Village or County
        Court whose structure this module does not encode -- naming one would
        be a guess, and naming the NYC court would be wrong."""
        if in_nyc:
            return court, None
        where = f"county {county!r}" if county else "an unknown county"
        return None, (f"{court} covers the five boroughs only, and this matter "
                      f"is in {where} -- outside New York City the equivalent "
                      f"forum is a City/Town/Village/County Court, whose "
                      f"structure this module does not encode")

    if case_type is CaseType.LANDLORD_TENANT:
        return _nyc_only("N.Y.C. Civil Court -- Housing Part")
    if case_type is CaseType.SMALL_CLAIM:
        if amount is None:
            return None, ("small claim with no amount_in_controversy -- cannot "
                          f"tell whether it is within the "
                          f"${NYC_SMALL_CLAIMS_LIMIT:,} small claims limit")
        if amount <= NYC_SMALL_CLAIMS_LIMIT:
            return _nyc_only("N.Y.C. Civil Court -- Small Claims Part")
        # Over the small-claims ceiling it is an ordinary civil money claim,
        # so fall through to that branch rather than reporting no forum.
        case_type = CaseType.CIVIL_MONEY
    if case_type is CaseType.CIVIL_MONEY:
        if amount is None:
            return None, ("civil money claim with no amount_in_controversy -- "
                          f"the ${NYC_CIVIL_COURT_LIMIT:,} Civil Court ceiling "
                          f"is what separates Civil from Supreme Court")
        if amount <= NYC_CIVIL_COURT_LIMIT:
            return _nyc_only("N.Y.C. Civil Court -- General Civil")
        # Supreme Court sits in every county, so this one needs no NYC guard.
        return "N.Y. Supreme Court", None
    if case_type is CaseType.CRIMINAL_MISDEMEANOR:
        return _nyc_only("N.Y.C. Criminal Court")
    # Supreme, Family and Surrogate's Court exist statewide.
    if case_type is CaseType.CRIMINAL_FELONY:
        return "N.Y. Supreme Court (criminal term)", None
    if case_type is CaseType.FAMILY:
        return "N.Y. Family Court", None
    if case_type is CaseType.PROBATE:
        return "N.Y. Surrogate's Court", None
    return None, f"no routing rule for case_type={case_type.value!r}"


_BASIS_FIELDS = (
    ("domiciled_in_forum", JurisdictionBasis.DOMICILE),
    ("transacted_business_in_forum", JurisdictionBasis.TRANSACTION_OF_BUSINESS),
    ("contracted_to_supply_in_forum", JurisdictionBasis.CONTRACT_TO_SUPPLY_IN_FORUM),
    ("tortious_act_in_forum", JurisdictionBasis.TORTIOUS_ACT_IN_FORUM),
    ("owns_real_property_in_forum", JurisdictionBasis.REAL_PROPERTY_IN_FORUM),
)


def resolve_venue(q: VenueQuery) -> VenueResolution:
    """Map a location and case shape onto candidate forums. Deterministic, and
    fail-closed: an unsupported state resolves to nothing but a reason."""
    state = (q.state or "").strip().upper()
    if state != "NY":
        return VenueResolution(
            state=state or None,
            unresolved=(f"no venue table for state {state or '(unset)'!r} -- "
                        "this module covers NY only",),
        )

    sources: list[SourceNote] = []
    unresolved: list[str] = []

    county = (q.county or "").strip() or None
    if q.borough is not None:
        county = BOROUGH_COUNTY[q.borough]
        sources.append(BOROUGH_COUNTY_SOURCE)
    if county is None:
        unresolved.append("no borough or county supplied -- county, federal "
                          "district and circuit not determined")

    district = circuit = None
    if county is not None:
        district = federal_district_for_county(county)
        sources.append(FEDERAL_DISTRICT_SOURCE)
        if district is None:
            unresolved.append(
                f"county {county!r} is not in this module's SDNY/EDNY tables -- "
                "it is most likely NDNY or WDNY, whose county lists are not "
                "encoded here")
        else:
            circuit = DISTRICT_CIRCUIT[district]
            sources.append(CIRCUIT_SOURCE)

    court, why = _state_court(q.case_type, q.amount_in_controversy, county)
    if court is not None:
        sources.append(COURT_ROUTING_SOURCE)
    if why:
        unresolved.append(why)

    bases = tuple(basis for attr, basis in _BASIS_FIELDS if getattr(q, attr))
    if bases:
        sources.append(LONG_ARM_SOURCE)

    # De-dup while preserving order: several branches cite the same table and
    # a resolution should list each source once.
    seen: list[SourceNote] = []
    for s in sources:
        if s not in seen:
            seen.append(s)

    return VenueResolution(
        state=state, borough=q.borough, county=county, state_court=court,
        federal_district=district, circuit=circuit, bases=bases,
        unresolved=tuple(unresolved), sources=tuple(seen),
    )
