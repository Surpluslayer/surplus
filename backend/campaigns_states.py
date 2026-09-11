"""campaigns_states.py : the worklist — every priority state, what it publishes,
and how far its adapter has got.

WHY THIS IS CODE AND NOT A MARKDOWN TABLE. A list of states in a document goes
stale the moment an adapter lands and nobody edits the doc; this one reads the
live registry, so "written" and "configured" are observed rather than claimed.
`status()` is the answer to "where are we", and it cannot lie about which
adapters exist because it asks campaigns_sources rather than a hand-kept field.

WHAT A PROFILE IS FOR. Before writing a state's adapter you want to know three
things: what shape it publishes (a PDF needs a parser, a Socrata portal needs
an id), whether anyone has confirmed that, and which of its office names will
break the shared normaliser. The first two order the work. The third is the one
that silently corrupts data if missed -- Pennsylvania's "Representative in the
General Assembly" filed every statehouse candidate as a U.S. House candidate
until it was mapped -- so `office_trap` records it per state, in advance,
whether or not the adapter exists yet.

CONFIDENCE IS RECORDED, NOT ASSUMED. `publication` says how well the format is
actually known:

    confirmed  a specific document or portal was identified
    likely     the state is known to run a platform that probably carries it
    assumed    a shape was ASSUMED so the generic adapter could be wired.
               Nobody has checked. The adapter will refuse to run until
               someone sets a dataset, so an assumption cannot quietly become
               a fetch -- but it is recorded as an assumption, not a finding.
    unknown    nobody has looked, and no shape is claimed

The distinction between "assumed" and "confirmed" is the one that matters when
someone picks up a state: writing "file" for a state nobody has opened would
make the worklist look finished and send them down the wrong path, so the
level says plainly which it is. A test enforces that "unknown" never carries
a shape.

None of this fetches anything. It is a map of the territory, kept beside the
code that crosses it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import campaigns_races as races
from . import campaigns_sources as sources
from .campaigns_filings import FieldMap


@dataclass(frozen=True)
class StateProfile:
    """What is known about one state's candidate publishing."""
    state: str
    publication: str          # "confirmed" | "likely" | "assumed" | "unknown"
    shape: str                # "pdf" | "socrata" | "file" | "search-form" | "unknown"
    source_page: str = ""
    note: str = ""
    # The office wording that will be misfiled if it is not mapped explicitly.
    office_trap: str = ""
    # Anything about the state's institutions that changes what a row means.
    quirk: str = ""

    # --- the parts campaigns_generic needs to actually read the state -------
    # Where the table is: a Socrata resource id, or a file URL. Empty means
    # nobody has identified it and the adapter refuses to run.
    dataset: str = ""
    socrata_domain: str = ""
    # This state's wording -> canonical office. Best-effort and unverified
    # against the real file; a missing wording DROPS a row rather than
    # misfiling it, except for statehouse names lacking the word "state",
    # which is why those are pinned. probe() reports what went unmapped.
    office_names: dict = field(default_factory=dict)
    fields: FieldMap = field(default_factory=FieldMap)
    # A candidate may hold several rows, one per party line. See
    # campaigns_generic on fusion voting -- off everywhere else, where two
    # rows for one seat is a bug worth seeing.
    fusion_voting: bool = False

    @property
    def is_tabular(self) -> bool:
        """PDF states need their own parser; these can use the generic path."""
        return self.shape in ("socrata", "file")


# The federal offices, worded the way most states word them. Merged into each
# state's own map so a profile only has to carry what is unusual about it.
_FEDERAL: dict[str, str] = {
    "united states representative": "U.S. House",
    "u.s. representative": "U.S. House",
    "us representative": "U.S. House",
    "representative in congress": "U.S. House",
    "representative to congress": "U.S. House",
    "united states senator": "U.S. Senate",
    "u.s. senator": "U.S. Senate",
    "us senator": "U.S. Senate",
    "governor and lieutenant governor": "Governor",
    "governor": "Governor",
}

# The ordinary statehouse wording, for states that DO say "state".
_PLAIN_STATEHOUSE: dict[str, str] = {
    "state senator": "State Senate",
    "state senate": "State Senate",
    "state representative": "State House",
    "state house": "State House",
}


def _offices(*extra: dict) -> dict[str, str]:
    """Federal wordings plus whatever this state does differently."""
    merged = dict(_FEDERAL)
    for block in extra:
        merged.update(block)
    return merged


PROFILES: dict[str, StateProfile] = {
    "CA": StateProfile(
        state="CA", publication="confirmed", shape="pdf",
        source_page="https://elections.cdn.sos.ca.gov/statewide-elections/"
                    "2026-general/cert-list-candidates.pdf",
        note="Certified list, certified 2026-08-27. URL confirmed; layout not "
             "yet read. Cal-Access deliberately not used -- finance data, "
             "different question, and use restrictions unresolved.",
        office_trap="'MEMBER OF THE STATE ASSEMBLY' is the state house.",
    ),
    "PA": StateProfile(
        state="PA", publication="confirmed", shape="socrata",
        socrata_domain="data.pa.gov",
        source_page="https://data.pa.gov/",
        note="Socrata portal confirmed; 2026 dataset id NOT identified. "
             "campaigns_pa.discover() searches the catalogue for it. "
             "RULED OUT: 53wp-ib3s 'Campaign Finance Disclosure Filer Data' "
             "is the obvious catalogue hit and is the WRONG UNIVERSE -- it "
             "lists whoever filed an expense report, across cycles, including "
             "committees and lobbyists, and omits any candidate who filed to "
             "appear on the ballot without reporting. It would parse, produce "
             "records, and be a different set of people than the 2026 ballot.",
        office_trap="'Representative in the General Assembly' is the STATE "
                    "house and 'Senator in the General Assembly' the state "
                    "senate. Pennsylvania never says 'state', which is the "
                    "word the shared normaliser relies on -- unmapped, every "
                    "statehouse candidate files as U.S. House.",
    ),
    "OH": StateProfile(
        state="OH", publication="confirmed", shape="file",
        source_page="https://www.ohiosos.gov/elections/",
        note="Publishes through press releases and a spreadsheet whose URL "
             "changes each cycle. No catalogue to search, so finding the file "
             "is a human step.",
        office_trap="'Representative to Congress' is federal.",
        quirk="Governor and Lieutenant Governor run as a joint ticket, so one "
              "name cell can hold two people.",
    ),
    "AZ": StateProfile(
        state="AZ", publication="confirmed", shape="pdf",
        source_page="https://azsos.gov/elections/candidates",
        note="Publishes PDFs. The primary-ballot file is NOT the general one; "
             "reading it would return candidates who lost in July.",
        office_trap="'State Representative' vs 'Representative in Congress'.",
        quirk="FIRST lieutenant governor election, Nov 2026, on a joint ticket "
              "with the governor (Prop 131, 2022; running mates due 2026-09-04). "
              "No prior cycle's file exists to check the gubernatorial rows "
              "against, so that wording is the least-confirmed thing about AZ.",
    ),
    "TX": StateProfile(
        state="TX", publication="likely", shape="file",
        source_page="https://www.sos.state.tx.us/elections/candidates/index.shtml",
        note="The largest prize on this list: 40 seats -- 38 districts, the "
             "Class 2 Senate seat and the governorship -- behind one office. "
             "The SOS publishes 'Candidate Listing Information' for the "
             "November 3 general election; whether that is a downloadable file "
             "or only a web page is UNCONFIRMED, so `dataset` stays empty and "
             "the adapter refuses rather than guessing a URL.",
        office_trap="'State Representative' and 'State Senator' against the "
                    "federal offices. Texas does say 'state', so the shared "
                    "rule would cope, but both are pinned anyway.",
        quirk="Texas elects its lieutenant governor SEPARATELY, not on a joint "
              "ticket -- so a Texas governor row carries one name. Harmless "
              "either way: split_ticket is a no-op without a conjunction.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
    "NY": StateProfile(
        state="NY", publication="likely", shape="socrata",
        socrata_domain="data.ny.gov",
        source_page="https://data.ny.gov/",
        note="New York runs a Socrata portal, so IF the 2026 candidate list is "
             "published there this is a resource id and nothing else. "
             "Unconfirmed; the State Board of Elections is the fallback and "
             "would make this a 'file' state instead.",
        office_trap="'Member of Assembly' is the STATE house and never says "
                    "'state' -- exactly Pennsylvania's trap, which silently "
                    "filed every statehouse candidate as U.S. House until it "
                    "was pinned. Pinned here before the first run.",
        quirk="FUSION VOTING. A New York candidate may run on several party "
              "lines at once -- a major party plus Conservative or Working "
              "Families -- and appears as a SEPARATE ROW per line. Parsed "
              "naively New York reports roughly half again as many candidates "
              "as are running, and the extra rows are not malformed, so "
              "nothing flags them. fusion_voting collapses them on the "
              "identity key and reports the count folded.",
        office_names=_offices(_PLAIN_STATEHOUSE, {
            "member of assembly": "State House",
            "state assembly": "State House",
            "assembly": "State House",
        }),
        fusion_voting=True,
    ),
    "NC": StateProfile(
        state="NC", publication="likely", shape="file",
        source_page="https://www.ncsbe.gov/results-data",
        note="The State Board of Elections publishes bulk data files rather "
             "than a portal or a search form, which if it holds for candidate "
             "lists makes this one of the easier states. Unconfirmed. No "
             "governor's race: Stein was elected in 2024.",
        office_trap="'NC House of Representatives' and 'NC Senate' use the "
                    "state ABBREVIATION where the shared rule looks for the "
                    "word 'state' -- so 'NC House of Representatives' would "
                    "fall through to U.S. House. Pinned.",
        office_names=_offices(_PLAIN_STATEHOUSE, {
            "nc house of representatives": "State House",
            "nc senate": "State Senate",
            "n.c. house of representatives": "State House",
            "n.c. senate": "State Senate",
            "us house of representatives": "U.S. House",
        }),
    ),
    "MI": StateProfile(
        state="MI", publication="assumed", shape="file",
        source_page="https://www.michigan.gov/sos/elections",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding; dataset stays "
             "empty so the adapter refuses until someone looks.",
        office_trap="'Representative in Congress' is federal. Michigan does "
                    "say 'state' for its statehouse, so the shared rule would "
                    "cope, but both are pinned.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
    "WA": StateProfile(
        state="WA", publication="likely", shape="socrata",
        socrata_domain="data.wa.gov",
        source_page="https://data.wa.gov/",
        note="Washington runs a Socrata portal and the Secretary of State "
             "publishes candidate data; which carries the 2026 list is "
             "unconfirmed. No Senate or governor race -- 10 House seats only.",
        office_trap="A 'Legislative District' number is shared by both state "
                    "chambers, so district alone does not identify a seat; the "
                    "office column is what separates them.",
        quirk="Top-two primary: the November ballot can carry two candidates "
              "of the SAME party for one seat. Nothing here depends on party, "
              "so it changes no parsing -- noted so two same-party rows for "
              "one district do not read as a duplicate.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
    "CO": StateProfile(
        state="CO", publication="assumed", shape="file",
        source_page="https://www.coloradosos.gov/pubs/elections/",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding.",
        office_names=_offices(_PLAIN_STATEHOUSE, {
            "representative to the united states congress": "U.S. House",
        }),
    ),
    "IA": StateProfile(
        state="IA", publication="assumed", shape="file",
        source_page="https://sos.iowa.gov/elections/",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
    "NE": StateProfile(
        state="NE", publication="assumed", shape="file",
        source_page="https://sos.nebraska.gov/elections/",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding.",
        office_trap="There is NO state house. A Nebraska 'State Senator' sits "
                    "in the only chamber -- mapping it to State Senate is "
                    "right, but waiting for a State House row is not, and a "
                    "coverage check expecting one would read as a thin parse.",
        quirk="The Legislature is UNICAMERAL and officially NONPARTISAN, so "
              "its rows carry no party at all. Harmless, since party is "
              "discarded by design -- but an empty party column here is not "
              "evidence that the parse failed.",
        office_names=_offices({
            "state senator": "State Senate",
            "member of the legislature": "State Senate",
            "legislature": "State Senate",
        }),
    ),
    "NM": StateProfile(
        state="NM", publication="assumed", shape="file",
        source_page="https://www.sos.nm.gov/voting-and-elections/",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
    "ME": StateProfile(
        state="ME", publication="assumed", shape="file",
        source_page="https://www.maine.gov/sos/cec/elec/",
        note="Publishing format NOT investigated. 'file' is the assumption "
             "that wires the generic adapter, not a finding. Smallest "
             "toss-up state at 4 seats.",
        quirk="Ranked-choice voting for federal races, and NO lieutenant "
              "governor -- Maine is one of the few states without the office, "
              "so a Maine governor row carries exactly one name. Neither "
              "changes this parse; both are noted so nobody goes looking for a "
              "problem that is not here.",
        office_names=_offices(_PLAIN_STATEHOUSE),
    ),
}


# States with a bespoke module. The rule: a state gets its own file when it
# needs a PARSER (California and Arizona publish PDFs) or its own discovery
# helper; otherwise it is a profile plus campaigns_generic. Pennsylvania and
# Ohio predate the generic path and keep their modules -- they work and are
# tested, and churning them to prove a point is not worth a regression.
BESPOKE_ADAPTERS: frozenset[str] = frozenset({"CA", "PA", "OH", "AZ"})


def profile(state: str) -> Optional[StateProfile]:
    return PROFILES.get((state or "").strip().upper())


def register_profile_adapters() -> list[str]:
    """Wire every tabular state that has no module of its own.

    Called at import, so importing this module is enough to make the generic
    states reachable through campaigns_sources.gather().
    """
    from . import campaigns_generic

    wired: list[str] = []
    for state, prof in PROFILES.items():
        if state in BESPOKE_ADAPTERS or not prof.is_tabular:
            continue
        sources.register_filing_source(state, campaigns_generic.build_adapter(prof))
        wired.append(state)
    return sorted(wired)


def status() -> dict[str, object]:
    """Where the worklist stands, read from the live registry.

    "written" means an adapter is registered. "configured" means it will
    actually return rows -- an adapter whose dataset id or document URL is
    still unset is written but not configured, and reporting those as the same
    thing is how a coverage number starts describing reach instead of data.
    """
    sources.load_state_adapters()
    registered = set(sources.STATE_FILING_SOURCES)

    rows: list[dict] = []
    for state in sorted(PROFILES, key=lambda s: -len(races.seats_for_state(s))):
        prof = PROFILES[state]
        seats = races.seats_for_state(state)
        rows.append({
            "state": state,
            "seats": len(seats),
            "house": sum(1 for s in seats if s.office == "U.S. House"),
            "senate": sum(1 for s in seats if s.office == "U.S. Senate"),
            "governor": sum(1 for s in seats if s.office == "Governor"),
            "publication": prof.publication,
            "shape": prof.shape,
            "adapter_written": state in registered,
            "configured": _is_configured(state),
            "office_trap": bool(prof.office_trap),
            "quirk": bool(prof.quirk),
        })

    written = [r["state"] for r in rows if r["adapter_written"]]
    configured = [r["state"] for r in rows if r["configured"]]
    return {
        "states": rows,
        "written": written,
        "configured": configured,
        "reach": races.coverage(written),
        "actual": races.coverage(configured),
        "adapter_load_errors": dict(sources.ADAPTER_LOAD_ERRORS),
    }


def _is_configured(state: str) -> bool:
    """Does this state's adapter have a source to read?

    Asks each module for its own location constant rather than keeping a flag
    here, so filling one in is a one-line edit that this report picks up
    immediately and cannot disagree with.
    """
    import importlib
    locations = {"CA": ("campaigns_ca", "CERTIFIED_LIST_URL"),
                 "PA": ("campaigns_pa", "DATASET_ID"),
                 "OH": ("campaigns_oh", "DOWNLOAD_URL"),
                 "AZ": ("campaigns_az", "DOCUMENT_URL")}
    entry = locations.get(state)
    if not entry:
        # A profile-driven state is configured when its dataset is filled in.
        prof = PROFILES.get(state)
        return bool(prof and prof.is_tabular and prof.dataset)
    module_name, attribute = entry
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except Exception:                                 # noqa: BLE001
        return False
    return bool(getattr(module, attribute, ""))


def next_states(limit: int = 5) -> list[dict]:
    """States with no adapter at all, most seats first. Empty once every
    priority state is wired -- at which point unconfigured_states() is the
    list that matters."""
    sources.load_state_adapters()
    registered = set(sources.STATE_FILING_SOURCES)
    pending = sorted((s for s in PROFILES if s not in registered),
                     key=lambda s: (-len(races.seats_for_state(s)), s))
    return [_worklist_row(s) for s in pending[:limit]]


def unconfigured_states(limit: int = 20) -> list[dict]:
    """Written but not yet pointed at a document, most seats first.

    This is the real remaining work once every state has an adapter: an
    adapter with no dataset returns nothing, so reach without configuration
    describes what the code could read rather than what it can.
    """
    pending = sorted((s for s in PROFILES if not _is_configured(s)),
                     key=lambda s: (-len(races.seats_for_state(s)), s))
    return [_worklist_row(s) for s in pending[:limit]]


def _worklist_row(state: str) -> dict:
    prof = PROFILES[state]
    if prof.shape == "socrata":
        how = f"campaigns_generic.discover('{state}') to find the resource id"
    elif prof.shape == "pdf":
        how = f"find the general-election PDF at {prof.source_page}"
    else:
        how = f"find the candidate file at {prof.source_page}"
    return {"state": state,
            "seats": len(races.seats_for_state(state)),
            "shape": prof.shape,
            "publication": prof.publication,
            "source_page": prof.source_page,
            "office_trap": prof.office_trap,
            "how": how}


register_profile_adapters()


# ---------------------------------------------------------------------------
# THE REST OF THE COUNTRY
#
# The fourteen states above carry the competitive House races and were worked
# one at a time. These thirty-six complete the map: with them, PROFILES covers
# every state on the 2026 ballot, so "find every candidate in the midterm" has
# an adapter for each of the 504 federal and gubernatorial seats rather than
# for the toss-up subset.
#
# WHAT IS REAL HERE AND WHAT IS NOT. Seats and schedule are not guessed -- they
# come from campaigns_races, which is law and arithmetic-tested. The office
# TRAPS below are researched and are the actual value of this block: they are
# the wordings that would be silently misfiled, and finding them after the fact
# means finding them by noticing a number was wrong. Everything else --
# publishing format, dataset location, column names -- is UNINVESTIGATED, which
# is why every one of these ships as publication="assumed" with an empty
# dataset. The adapter refuses to run until someone fills that in, so an
# assumption cannot quietly become a fetch.
#
# THE TRAPS, AND WHY EACH ONE MATTERS
#
#   "General Court" (MA, NH) is the worst of them. A Massachusetts statehouse
#   candidate runs for "Representative in General Court" -- which contains
#   "Representative", does not contain "state", and therefore falls through the
#   shared normaliser straight into U.S. House. Exactly Pennsylvania's bug, in
#   two more states, and neither says anything that looks wrong in a log.
#
#   "House of Delegates" (MD, WV, VA) fails the same way: "House" matches, no
#   "state" excludes it, so every delegate becomes a congressional candidate.
#
#   "Assembly" (NV, WI) fails the OTHER way. It matches nothing at all, so the
#   rows are dropped rather than misfiled -- a silent undercount instead of a
#   silent miscount. Less damaging, equally invisible.
# ---------------------------------------------------------------------------

# Massachusetts and New Hampshire: the legislature is the General Court.
_GENERAL_COURT: dict[str, str] = {
    "representative in general court": "State House",
    "state representative in general court": "State House",
    "senator in general court": "State Senate",
    "state senator in general court": "State Senate",
    "general court": "State House",
}

# Maryland, West Virginia, Virginia: a lower house of Delegates.
_DELEGATES: dict[str, str] = {
    "house of delegates": "State House",
    "delegate": "State House",
    "state delegate": "State House",
}

# Nevada, Wisconsin: an Assembly that does not say "state".
_ASSEMBLY: dict[str, str] = {
    "general assembly": "State House",
    "assembly": "State House",
    "assembly member": "State House",
    "member of the assembly": "State House",
    "state assembly": "State House",
}

_ASSUMED_NOTE = ("Publishing format NOT investigated. 'file' is the assumption "
                 "that wires the generic adapter, not a finding; dataset stays "
                 "empty so the adapter refuses until someone looks.")

# state -> (statehouse wording block, office_trap, quirk)
_REST: dict[str, tuple] = {
    "MA": (_GENERAL_COURT,
           "The legislature is the GENERAL COURT: 'Representative in General "
           "Court' is the state house. It contains 'Representative' and not "
           "'state', so unmapped it files as U.S. House -- Pennsylvania's bug "
           "again.", ""),
    "NH": (_GENERAL_COURT,
           "General Court, as in Massachusetts: 'Representative in General "
           "Court' is the state house and would otherwise read as federal.",
           "No lieutenant governor, so a governor row carries one name."),
    "MD": (_DELEGATES,
           "'House of Delegates' is the state house. 'House' matches the "
           "federal pattern and nothing excludes it, so every delegate would "
           "file as a U.S. House candidate.", ""),
    "WV": (_DELEGATES,
           "'House of Delegates' is the state house -- same trap as Maryland.",
           ""),
    "VA": (_DELEGATES,
           "'House of Delegates' is the state house -- same trap as Maryland.",
           "No legislative elections in 2026: Virginia runs them in odd years, "
           "so a run finding zero statehouse candidates here is correct."),
    "NV": (_ASSEMBLY,
           "The lower house is the 'Assembly' with no 'state' in front. That "
           "matches NEITHER pattern, so its rows are dropped rather than "
           "misfiled: a silent undercount, not a silent miscount.", ""),
    "WI": (_ASSEMBLY,
           "'Assembly' without 'state' -- dropped rather than misfiled, as in "
           "Nevada.", ""),
    "LA": (_PLAIN_STATEHOUSE, "",
           "JUNGLE PRIMARY: every candidate of every party appears on the "
           "November ballot together, with a December runoff if nobody clears "
           "50%. So Louisiana's 'general election' field is the whole field, "
           "not two nominees -- expect many more candidates per seat than "
           "elsewhere, which is not a duplicate-row bug. No legislative "
           "elections in 2026 (odd-year state)."),
    "AK": (_PLAIN_STATEHOUSE, "",
           "Top-four primary and ranked-choice general, so the November ballot "
           "can carry four candidates for one seat, several of the same party. "
           "Changes nothing here -- party is discarded by design -- but four "
           "rows for one at-large seat is correct, not a parse fault."),
    "MS": (_PLAIN_STATEHOUSE, "",
           "No legislative elections in 2026 (odd-year state)."),
    "NJ": (_ASSEMBLY,
           "The lower house is the 'General Assembly', which does not say "
           "'state' -- the Pennsylvania family of trap.",
           "No legislative elections in 2026 (odd-year state)."),
    "TN": (_PLAIN_STATEHOUSE,
           "The body is the General Assembly but its members are styled State "
           "Representative and State Senator, so the plain mapping holds. "
           "Worth confirming on the first real run.", ""),
}

for _state in sorted(races.STATES - set(PROFILES)):
    _house, _trap, _quirk = _REST.get(_state, (_PLAIN_STATEHOUSE, "", ""))
    PROFILES[_state] = StateProfile(
        state=_state,
        publication="assumed",
        shape="file",
        source_page=f"https://www.usa.gov/state-election-office-{_state.lower()}",
        note=_ASSUMED_NOTE,
        office_trap=_trap,
        quirk=_quirk,
        office_names=_offices(_house),
    )

register_profile_adapters()
