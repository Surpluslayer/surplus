"""
civic_geo.py : the stack of governments standing over one point.

Every address in the United States sits inside seven or eight separate
governments at once, each with its own election, its own money and its own
powers. Residents experience them as one blur -- "the city", "the government"
-- which is exactly why nobody knows that the body raising their water rate is
elected in an off-cycle race nobody covers.

This module names the stack for a point, and says what each layer actually
decides. The map uses it as a lens: pick a layer, see the boundary you are
inside, and ask that layer's question in that layer's vocabulary.

Two keyless sources:

  * the US Census Geocoder, which answers "which congressional district,
    state legislative districts, county, place and school districts contain
    this point" in one call, and
  * OpenStreetMap's is_in, which adds the council district (a `political`
    boundary the Census does not carry) and how the land at that point is
    actually used -- plus the relation ids the map needs to draw an outline.

What is deliberately NOT here: the legal zoning code. OpenStreetMap knows how
land is used, not what an ordinance permits, and pretending otherwise would be
the same unsourced confidence the rest of this surface refuses. The layer names
the authority and the question asks it for the code.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
# The Census publishes the shapes of the districts it names. Same agency, same
# vintage, keyless -- so the outline of a congressional or state legislative
# district comes from the body that drew it rather than from a name guess.
TIGER_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services"
             "/TIGERweb/tigerWMS_Current/MapServer")
# Overpass is run by volunteers and the main instance refuses connections
# whenever it is busy, which on this surface meant losing the council district,
# the land under the pin and every OpenStreetMap outline at once. The public
# mirrors run the same API over the same data, so a refusal is a reason to ask
# the next one rather than to tell the reader nothing is there.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)
OVERPASS_URL = OVERPASS_URLS[0]          # kept for callers that name one host
# A busy Overpass hangs rather than refusing, so the per-mirror wait is short:
# giving one overloaded host 12 seconds is 12 seconds not spent asking a host
# that would have answered. The budget caps the whole chain.
OVERPASS_TIMEOUT_S = 9.0
OVERPASS_BUDGET_S = 22.0
TIMEOUT_S = 12.0

# The Census geocoder is routinely slower than any other source here -- ten to
# twenty seconds is ordinary for it -- and it runs in parallel with Overpass,
# so waiting longer costs nothing but the patience of one thread. A 12s cap
# was quietly turning every one of its answers into a timeout.
CENSUS_TIMEOUT_S = 30.0
CACHE_TTL_S = 900

USER_AGENT = "surplus-civic/1.0 (+https://event.surpluslayer.com/civic)"


# ---------------------------------------------------------------------------
# What each layer decides. This is the part a resident cannot look up.
# ---------------------------------------------------------------------------

LAYERS: dict = {
    "congress": {
        "label": "U.S. House district",
        "color": "#C77FD0",
        "elected": "Every 2 years",
        "decides": [
            "Federal tax law, including the mortgage-interest and SALT deductions",
            "Housing vouchers and public-housing money",
            "Medicare, and the federal share of Medicaid",
            "The formulas that send highway, transit and broadband money to your state",
        ],
        "asks": "What has {name} voted on lately that reaches {place} — housing, "
                "tax, energy or transport — and what is moving in Congress now?",
    },
    "state_upper": {
        "label": "State Senate district",
        "color": "#7FB0D0",
        "elected": "Usually every 4 years",
        "decides": [
            "Whether cities may cap rents at all — state preemption settles that",
            "What a city is allowed to refuse to permit (zoning preemption)",
            "The school funding formula, and the property-tax rules underneath it",
            "Utility regulation: who pays for grid upgrades and how rates are set",
        ],
        "asks": "What bills in {name} would change rents, property tax, schools or "
                "utility bills in {place}, and where are they now?",
    },
    "state_lower": {
        "label": "State House district",
        "color": "#8FBF9F",
        "elected": "Usually every 2 years",
        "decides": [
            "The same statutes as the upper chamber — both must pass a bill",
            "Budget line items that reach individual cities and districts",
            "Housing production mandates and their exemptions",
        ],
        "asks": "What bills in {name} would change housing, tax or schools in "
                "{place}, and what is on the floor now?",
    },
    "place": {
        "label": "City",
        "color": "#5FD3A0",
        "elected": "Mayor and council",
        "decides": [
            "The zoning code: what may be built, how tall, how many homes",
            "Building permits, inspections and code enforcement",
            "Police and fire budgets, streets, rubbish, parking",
            "Local business taxes, fees and the rent board where one exists",
        ],
        "asks": "What is {name} deciding now about zoning, housing, permits or "
                "local taxes, and what changed in the last two years?",
    },
    "council": {
        "label": "Council district",
        "color": "#EEA76B",
        "elected": "Your single seat on the council",
        "decides": [
            "Your one vote on every city decision above",
            "Usually the first hearing a project in your street must pass",
            "Discretionary money for the district itself",
        ],
        "asks": "What is on the agenda for {name} in {place} — projects, hearings, "
                "and votes affecting this neighbourhood?",
    },
    "landuse": {
        "label": "Land use here",
        "color": "#A0A8B0",
        "elected": "Set by the city's zoning code",
        "decides": [
            "How this land is used today, as mapped by OpenStreetMap",
            "The legal zoning code is the city's, and is not in this data",
        ],
        "asks": "Which zoning district covers {place}, what does that section of "
                "the municipal code permit — height, density, parking — and what "
                "change to it is proposed?",
    },
}

# What to look up on each body's own site. The two that matter most are the
# assessor's record (the actual number behind a tax bill) and the permit
# portal (the actual application behind a building site) -- neither of which
# any search will produce as reliably as the reader clicking through.
LOOKUPS = {
    "place": "Search the permit and planning portal for your address, and the "
             "zoning designation the planning department publishes for it",
    "council": "Find your council member's page and the agenda for the next meeting",
    "state_upper": "Look up your senator and the bills they have authored this session",
    "state_lower": "Look up your representative and the bills they have authored",
    "congress": "Look up your representative's votes and the bills they sponsor",
}

# The order the map offers them in: closest to a resident's daily life first.
LAYER_ORDER = ["landuse", "council", "place",
               "state_lower", "state_upper", "congress"]

# Census Geocoder names its geography collections in prose ; match loosely so a
# vintage change does not silently empty a layer.
# Matched as (must contain ALL of these) so a vintage prefix cannot break it:
# the Census labels these collections "119th Congressional Districts" and
# "2024 State Legislative Districts - Upper", which is why looking for the
# phrase "upper chamber" found nothing even when the call succeeded.
_CENSUS_KEYS = {
    "congress": (("congressional",),),
    "state_upper": (("legislative", "upper"),),
    "state_lower": (("legislative", "lower"),),
    "place": (("incorporated places",), ("census designated places",)),
}

# Which TIGERweb layer holds each lens, matched on the words in its published
# name rather than a layer number: the numbers move with every vintage, the
# names do not. Ordered -- the first that answers for a GEOID wins.
_TIGER_KEYS = {
    "congress": (("congressional", "districts"),),
    "state_upper": (("legislative", "districts", "upper"),),
    "state_lower": (("legislative", "districts", "lower"),),
    "place": (("incorporated", "places"), ("census", "designated", "places")),
}

_CACHE: dict = {}


# Which mirror answered last. A host that just worked is the best guess at the
# host that will work next, and it costs nothing to remember.
_OVERPASS_FIRST = 0


def overpass(query: str, timeout_s: float = OVERPASS_TIMEOUT_S) -> dict:
    """Run one Overpass query, trying the mirrors until one answers.

    Starts from whichever mirror last worked, so a long-running process stops
    paying the failed connection to a busy host on every call.
    """
    global _OVERPASS_FIRST
    import httpx
    started = time.monotonic()
    order = [(_OVERPASS_FIRST + i) % len(OVERPASS_URLS)
             for i in range(len(OVERPASS_URLS))]
    last: Optional[Exception] = None
    for index in order:
        if last is not None and time.monotonic() - started > OVERPASS_BUDGET_S:
            break
        url = OVERPASS_URLS[index]
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, data={"data": query},
                                   headers={"user-agent": USER_AGENT})
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 : the next mirror may be up
            last = exc
            print(f"  [civic.overpass] {url.split('/')[2]} failed: "
                  f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        _OVERPASS_FIRST = index
        return body
    raise last or RuntimeError("no overpass mirror answered")


def _cached(key: str, produce):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = produce()
    if len(_CACHE) > 400:
        for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:120]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

# The geocoder takes a benchmark/vintage pair, and a wrong one is rejected
# rather than ignored. Rather than bet the rail on a single spelling, try the
# current pair, then the decennial one, then the plain call with no layer
# filter -- stopping at the first that returns geographies.
CENSUS_ATTEMPTS = (
    {"benchmark": "Public_AR_Current", "vintage": "Current_Current", "layers": "all"},
    {"benchmark": "Public_AR_Current", "vintage": "Census2020_Current", "layers": "all"},
    {"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
)

# Total wall clock for all of them. One slow source must not hold the card.
CENSUS_BUDGET_S = 45.0


def census_geographies(lat: float, lon: float) -> dict:
    """Every Census geography containing this point, in one keyless call.

    Tried more than once, because a single miss costs five of the eight layers
    on the rail -- everything a resident cannot name from memory. An error
    body is read and reported: the geocoder explains a bad benchmark or
    vintage in words, and that sentence is worth more than "it failed".
    """
    import httpx
    headers = {"user-agent": USER_AGENT, "accept": "application/json"}
    started = time.monotonic()
    last: Optional[Exception] = None

    for params in CENSUS_ATTEMPTS:
        if time.monotonic() - started > CENSUS_BUDGET_S:
            break
        query = dict(params, x=f"{lon:.6f}", y=f"{lat:.6f}", format="json")
        try:
            with httpx.Client(timeout=CENSUS_TIMEOUT_S) as client:
                resp = client.get(CENSUS_URL, params=query, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
            body = resp.json()
            # The geocoder reports bad parameters in the body, with a 200.
            errors = body.get("errors") or []
            if errors:
                raise RuntimeError("; ".join(str(e) for e in errors)[:160])
            found = (body.get("result") or {}).get("geographies") or {}
            if found:
                return found
            last = RuntimeError("no geographies for this point")
        except Exception as exc:  # noqa: BLE001 : tried again, then reported
            last = exc
            print(f"  [civic.census] {params.get('vintage')} failed: "
                  f"{type(exc).__name__}: {str(exc)[:160]}")
    raise last or RuntimeError("census geocoder gave nothing")


def osm_areas(lat: float, lon: float) -> list[dict]:
    """Boundaries containing the point, plus the land under it.

    Carries relation ids, which is what lets the map outline a district
    rather than merely name it.
    """
    query = (f"[out:json][timeout:20];is_in({lat:.6f},{lon:.6f})->.a;"
             f"rel(pivot.a);out tags;"
             f"(way(around:25,{lat:.6f},{lon:.6f})[\"landuse\"];"
             f" way(around:25,{lat:.6f},{lon:.6f})[\"building\"];);out tags 3;")
    return overpass(query).get("elements") or []


def tiger_layer_ids() -> list[tuple[int, str]]:
    """Every layer TIGERweb publishes, as (id, name).

    Read once and cached, because the whole point of reading it is to stop
    hard-coding layer numbers that change with each vintage.
    """
    def build():
        import httpx
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.get(TIGER_URL, params={"f": "json"},
                              headers={"user-agent": USER_AGENT,
                                       "accept": "application/json"})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return [(int(layer["id"]), str(layer.get("name") or ""))
                for layer in resp.json().get("layers") or []
                if layer.get("id") is not None]

    return _cached("tiger:layers", build)


def outline_by_geoid(layer_key: str, geoid: str, detail: str = "") -> dict:
    """The Census's own shape for a Census-named district.

    The geocoder names the districts nobody can name from memory and hands
    back no geometry, so the map knew you were in Congressional District 12
    and could not draw it. TIGERweb has the shape, keyed by the same GEOID
    the geocoder returned -- so the outline is the boundary the Census drew,
    not a boundary that happens to share a name.
    """
    # The GEOID goes into a where= clause. Census GEOIDs are digits and
    # nothing else, so anything else is rejected rather than escaped.
    geoid = (geoid or "").strip()
    if not geoid.isdigit() or len(geoid) > 20:
        return {}
    patterns = _TIGER_KEYS.get(layer_key)
    if not patterns:
        return {}
    # A point sits in exactly one of the three kinds of school district, and
    # the geocoder already said which. Ask that one first.
    low = (detail or "").lower()
    patterns = sorted(patterns, key=lambda p: not all(w in low for w in p))

    layers = tiger_layer_ids()
    tried = 0
    for pattern in patterns:
        for layer_id, layer_name in layers:
            if not _matches_collection(layer_name, (pattern,)) or tried >= 4:
                continue
            tried += 1
            shape = _tiger_query(layer_id, geoid)
            if shape:
                return shape
    return {}


def _tiger_query(layer_id: int, geoid: str) -> dict:
    """One TIGERweb layer, asked for one GEOID's geometry."""
    import httpx
    params = {"where": f"GEOID='{geoid}'", "outFields": "GEOID",
              "returnGeometry": "true", "outSR": "4326", "f": "geojson"}
    with httpx.Client(timeout=TIMEOUT_S + 8) as client:
        resp = client.get(f"{TIGER_URL}/{int(layer_id)}/query", params=params,
                          headers={"user-agent": USER_AGENT,
                                   "accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    body = resp.json()
    # ArcGIS reports a rejected query in the body, with a 200.
    if body.get("error"):
        raise RuntimeError(str(body["error"].get("message") or "query rejected")[:120])
    for feature in body.get("features") or []:
        geometry = feature.get("geometry") or {}
        if geometry.get("coordinates"):
            return geometry
    return {}


# ---------------------------------------------------------------------------
# Who currently holds the seat
# ---------------------------------------------------------------------------
# A district is an abstraction until it has a name and a face on it. The Census
# says which district ; these say who is in it right now, and both sources
# publish the seat itself rather than commentary about it.

GOVTRACK_URL = "https://www.govtrack.us/api/v2/role"
# The card leads with this, so it is the thing the reader is waiting on. A
# roster that takes twelve seconds is a card that reads as broken.
ROSTER_TIMEOUT_S = 8.0
OPENSTATES_PEOPLE_URL = "https://v3.openstates.org/people.geo"
# OpenStates publishes the same roster it serves from the keyed API as a plain
# per-state CSV, with no key at all. One small download per state, cached, and
# the Census already told us which district the point is in -- so a deploy
# without an API key still names your two state legislators.
OPENSTATES_CSV_URL = "https://data.openstates.org/people/current/{state}.csv"
# The lens a chamber belongs to, in OpenStates' vocabulary. One table, so the
# two rosters cannot drift apart on which chamber is which.
_LAYER_FOR_CHAMBER = {"upper": "state_upper", "lower": "state_lower"}

# The Census keys states by FIPS code ; every roster keys them by postal
# abbreviation. One table, no network call, no guessing from a place name.
_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}


def _person(name: str, role: str, party: str = "", url: str = "",
            source: str = "", since: str = "", pid: int = 0) -> dict:
    return {"name": " ".join((name or "").split())[:120], "role": role,
            "party": (party or "").strip()[:40], "url": (url or "")[:300],
            "source": source, "since": (since or "")[:10], "id": int(pid or 0)}


def congress_members(geoid: str) -> list[dict]:
    """The three people your congressional GEOID sends to Washington.

    GovTrack keys current roles by state and district, needs no key, and
    publishes the seat rather than an opinion about who should hold it. Both
    senators come too : they vote on the same bills as the House member and
    nobody's ballot separates them.
    """
    geoid = (geoid or "").strip()
    if not geoid.isdigit() or len(geoid) != 4:
        return []
    state, district = _FIPS.get(geoid[:2], ""), int(geoid[2:])
    if not state:
        return []
    import httpx
    people: list[dict] = []
    asks = [{"role_type": "representative", "district": district},
            {"role_type": "senator"}]

    def fetch(ask):
        params = dict(ask, current="true", state=state, limit=3)
        with httpx.Client(timeout=ROSTER_TIMEOUT_S) as client:
            resp = client.get(GOVTRACK_URL, params=params,
                              headers={"user-agent": USER_AGENT,
                                       "accept": "application/json"})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.json().get("objects") or []

    with ThreadPoolExecutor(max_workers=2) as pool:
        for objects in list(pool.map(fetch, asks)):
            for role in objects:
                who = role.get("person") or {}
                people.append(_person(
                    who.get("name") or "", role.get("title_long") or role.get("title") or "",
                    role.get("party") or "", who.get("link") or "",
                    "govtrack", (role.get("startdate") or ""),
                    who.get("id") or 0))
    return people


def state_legislators(lat: float, lon: float) -> dict:
    """Who sits for this point in each chamber of the state legislature.

    OpenStates answers by coordinate, which is the only way to get this right
    -- state legislative districts are the boundaries residents are least
    able to name. Needs a free OPENSTATES_API_KEY ; without one the card says
    so rather than inventing a name.
    """
    import os
    key = (os.environ.get("OPENSTATES_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("no OPENSTATES_API_KEY")
    import httpx
    with httpx.Client(timeout=ROSTER_TIMEOUT_S) as client:
        resp = client.get(OPENSTATES_PEOPLE_URL,
                          params={"lat": f"{lat:.6f}", "lng": f"{lon:.6f}"},
                          headers={"x-api-key": key, "user-agent": USER_AGENT,
                                   "accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    found: dict = {}
    for who in resp.json().get("results") or []:
        current = who.get("current_role") or {}
        chamber = str(current.get("org_classification") or "").lower()
        layer = _LAYER_FOR_CHAMBER.get(chamber)
        if not layer:
            continue
        title = current.get("title") or chamber.title()
        district = current.get("district")
        role = f"{title}, district {district}" if district else str(title)
        found.setdefault(layer, []).append(_person(
            who.get("name") or "", role, who.get("party") or "",
            who.get("openstates_url") or "", "openstates"))
    return found


def _district_of(geoid: str) -> str:
    """The district number inside a state legislative GEOID.

    State FIPS, then the district code. Most states number their districts, a
    few letter them (Alaska's "A", Vermont's "BEN"), so the digits are tried
    first and the raw code kept when they do not apply.
    """
    geoid = (geoid or "").strip()
    if not geoid.isdigit() or len(geoid) < 3:
        return ""
    code = geoid[2:]
    return str(int(code)) if code.isdigit() else code.lstrip("0")


def state_roster(state: str) -> list[dict]:
    """Every sitting legislator in one state, from the keyless CSV."""
    state = (state or "").strip().lower()
    if len(state) != 2 or not state.isalpha():
        return []

    def build():
        import csv
        import io
        import httpx
        with httpx.Client(timeout=ROSTER_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(OPENSTATES_CSV_URL.format(state=state),
                              headers={"user-agent": USER_AGENT})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        # A state roster is a few hundred rows. Anything far larger is not the
        # file we asked for, and is not worth parsing to find out.
        if len(resp.content) > 4_000_000:
            raise RuntimeError("roster too large")
        return list(csv.DictReader(io.StringIO(resp.text)))

    return _cached(f"roster:{state}", build)


def state_legislators_keyless(state: str, upper_geoid: str = "",
                              lower_geoid: str = "") -> dict:
    """The two state legislators for this point, without an API key."""
    wanted = {"upper": _district_of(upper_geoid), "lower": _district_of(lower_geoid)}
    if not any(wanted.values()):
        return {}
    found: dict = {}
    for row in state_roster(state):
        chamber = (row.get("current_chamber") or "").strip().lower()
        district = (row.get("current_district") or "").strip()
        target = wanted.get(chamber)
        if not target or not district or chamber not in _LAYER_FOR_CHAMBER:
            continue
        here = str(int(district)) if district.isdigit() else district.lstrip("0")
        if here != target:
            continue
        layer = _LAYER_FOR_CHAMBER[chamber]
        title = "Senator" if chamber == "upper" else "Representative"
        found.setdefault(layer, []).append(_person(
            row.get("name") or "", f"{title}, district {district}",
            row.get("current_party") or "",
            row.get("openstates_url") or row.get("sources") or "",
            "openstates"))
    return found


def _state_seats(lat: float, lon: float, any_geoid: str,
                 upper_geoid: str, lower_geoid: str) -> dict:
    """State legislators, keyless first.

    The CSV needs no key and is the same roster the keyed API serves, so it is
    tried first ; the keyed point lookup is the fallback for the case the CSV
    cannot cover -- a district code we could not match to a row.
    """
    import os
    state = _FIPS.get((any_geoid or "")[:2], "")
    if state:
        try:
            found = state_legislators_keyless(state, upper_geoid, lower_geoid)
            if found:
                return found
        except Exception as exc:  # noqa: BLE001 : the keyed path may still work
            print(f"  [civic.roster] {state} csv failed: {type(exc).__name__}")
    if not (os.environ.get("OPENSTATES_API_KEY") or "").strip():
        raise RuntimeError("no roster for this point")
    return state_legislators(lat, lon)


def officials(lat: float, lon: float, congress_geoid: str = "",
              upper_geoid: str = "", lower_geoid: str = "") -> dict:
    """Everyone this point elects that a public roster will name.

    Both rosters are asked at once and either may fail ; a chamber nobody
    could name comes back as the reason it could not, so the card can say
    "no roster" instead of showing an empty space that reads as "nobody".
    """
    def build():
        people: dict = {"by_layer": {}, "sources": {}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            house = pool.submit(congress_members, congress_geoid) \
                if congress_geoid else None
            state = pool.submit(_state_seats, lat, lon,
                                congress_geoid or upper_geoid or lower_geoid,
                                upper_geoid, lower_geoid)
            if house is not None:
                try:
                    got = house.result()
                    if got:
                        people["by_layer"]["congress"] = got
                    people["sources"]["govtrack"] = len(got)
                except Exception as exc:  # noqa: BLE001 : one roster down is not an outage
                    people["sources"]["govtrack"] = type(exc).__name__
            try:
                got = state.result()
                people["by_layer"].update(got)
                people["sources"]["openstates"] = sum(len(v) for v in got.values())
            except Exception as exc:  # noqa: BLE001
                people["sources"]["openstates"] = type(exc).__name__
        return people

    return _cached(f"who:{lat:.4f},{lon:.4f},{congress_geoid}:{upper_geoid}:{lower_geoid}", build)


VOTES_URL = "https://www.govtrack.us/api/v2/vote_voter"


def recent_votes(person_id: int, limit: int = 5) -> list[dict]:
    """How this member has voted lately, most recent first.

    The roll call is the record. "Voted Yea on H.R. 1" is a fact with a date
    and a link on it ; "cares about housing" is not, and this surface does not
    deal in the second kind.
    """
    person_id = int(person_id or 0)
    if person_id <= 0:
        return []
    import httpx
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.get(VOTES_URL,
                          params={"person": person_id, "order_by": "-created",
                                  "limit": max(1, min(int(limit), 10))},
                          headers={"user-agent": USER_AGENT,
                                   "accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    out = []
    for record in resp.json().get("objects") or []:
        vote = record.get("vote") or {}
        question = (vote.get("question") or "").strip()
        if not question:
            continue
        link = vote.get("link") or ""
        out.append({"how": (record.get("option") or {}).get("value") or "",
                    "what": question[:200],
                    "result": (vote.get("result") or "")[:80],
                    "date": (vote.get("created") or "")[:10],
                    "url": link[:300]})
    return out


# ---------------------------------------------------------------------------
# What the council is actually deciding
# ---------------------------------------------------------------------------
# A large share of US city councils run Legistar, which publishes the agenda,
# the meeting calendar and every piece of legislation as JSON, with no key.
# That is the literal answer to "what is my council deciding" -- and paying a
# ranked web search to go and find a PDF of the same agenda is both slower and
# worse. No model is involved: these are records, not judgements.

LEGISTAR_URL = "https://webapi.legistar.com/v1"

# Most councils are reachable at their own name. These are the ones that are
# not, and guessing would quietly return another city's agenda.
_LEGISTAR_SLUGS = {
    "san francisco": "sfgov",
    "new york": "nyc",
    "boston": "bostonma",
    "austin": "austintexas",
    "seattle": "seattle",
    "philadelphia": "phila",
    "washington": "dc",
    "san jose": "sanjose",
    "long beach": "longbeach",
    "saint paul": "stpaul",
    "st. paul": "stpaul",
}


def _slug_candidates(city: str) -> list[str]:
    """The Legistar client names a city might answer to, best guess first."""
    plain = "".join(ch for ch in (city or "").lower() if ch.isalnum() or ch == " ")
    plain = " ".join(plain.split())
    if not plain:
        return []
    known = _LEGISTAR_SLUGS.get(plain)
    if known:
        return [known]
    joined = plain.replace(" ", "")
    return [joined, f"cityof{joined}"][:2]


def _legistar(slug: str, path: str, params: dict) -> list:
    """One Legistar call. The slug lands in the URL path, so it is checked."""
    if not slug.isalnum() or len(slug) > 40:
        raise ValueError("bad legistar client")
    import httpx
    with httpx.Client(timeout=ROSTER_TIMEOUT_S, follow_redirects=True) as client:
        resp = client.get(f"{LEGISTAR_URL}/{slug}/{path}", params=params,
                          headers={"user-agent": USER_AGENT,
                                   "accept": "application/json"})
    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    found = resp.json()
    return found if isinstance(found, list) else []


def legistar_client(city: str) -> str:
    """The Legistar client name for a city, or "" if it does not run one.

    Both answers are cached. A city that is not on Legistar is the common
    case, and re-probing two URLs for it on every card is the sort of thing
    that makes a page feel broken.
    """
    def build():
        for slug in _slug_candidates(city):
            try:
                if _legistar(slug, "bodies", {"$top": 1}):
                    return slug
            except Exception as exc:  # noqa: BLE001 : try the next spelling
                print(f"  [civic.legistar] {slug}: {type(exc).__name__}")
        return ""

    return _cached(f"legistar:{(city or '').lower()}", build)


def _when(date: str, time: str = "") -> str:
    day = (date or "")[:10]
    return f"{day} {time}".strip() if day else ""


def _meetings(slug: str, limit: int) -> list[dict]:
    """The next meetings on the clerk's calendar.

    Asked with a date filter first, because a council's history is thousands
    of rows. Not every city's Legistar accepts the same OData spelling of a
    date, though, so a rejection falls back to the unfiltered list and drops
    the past here -- a calendar that loads is worth more than a tidy query.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        rows = _legistar(slug, "events", {
            "$top": limit, "$orderby": "EventDate",
            "$filter": f"EventDate ge datetime'{today}'"})
    except Exception as exc:  # noqa: BLE001 : the plain list still works
        print(f"  [civic.legistar] {slug} date filter refused "
              f"({type(exc).__name__}); asking without it")
        rows = [row for row in _legistar(slug, "events",
                                         {"$top": 60, "$orderby": "EventDate desc"})
                if (row.get("EventDate") or "")[:10] >= today]
        rows.sort(key=lambda r: r.get("EventDate") or "")

    out = []
    for row in rows[:limit]:
        # Legistar marks a special or amended meeting with a leading star.
        body = " ".join((row.get("EventBodyName") or "").split()).lstrip("* ")[:120]
        if not body:
            continue
        out.append({
            "body": body,
            "when": _when(row.get("EventDate"), row.get("EventTime") or ""),
            "where": " ".join((row.get("EventLocation") or "").split())[:120],
            "url": (row.get("EventInSiteURL") or row.get("EventAgendaFile") or "")[:400],
        })
    return out


def _matters(slug: str, limit: int) -> list[dict]:
    """The newest legislation the council has taken up."""
    out = []
    for row in _legistar(slug, "matters",
                         {"$top": limit, "$orderby": "MatterIntroDate desc"}):
        title = " ".join((row.get("MatterTitle") or row.get("MatterName") or "").split())
        if not title:
            continue
        out.append({
            "file": " ".join((row.get("MatterFile") or "").split())[:40],
            "what": title[:240],
            "kind": " ".join((row.get("MatterTypeName") or "").split())[:60],
            "status": " ".join((row.get("MatterStatusName") or "").split())[:60],
            "when": (row.get("MatterIntroDate") or "")[:10],
        })
    return out


def council_agenda(city: str, limit: int = 4) -> dict:
    """The next meetings and the newest legislation, straight from the clerk.

    The calendar and the docket are asked for separately and fail separately:
    a city whose events endpoint is unhappy still has a docket worth showing,
    and losing both because one of them errored is the sort of blank that
    reads as "nothing is happening here".
    """
    slug = legistar_client(city)
    if not slug:
        return {"meetings": [], "matters": [], "source": ""}
    limit = max(1, min(int(limit), 10))

    found: dict = {"meetings": [], "matters": [], "source": "legistar",
                   "client": slug}
    for key, fetch in (("meetings", _meetings), ("matters", _matters)):
        try:
            found[key] = fetch(slug, limit)
        except Exception as exc:  # noqa: BLE001 : the other half still stands
            found.setdefault("partial", []).append(f"{key}:{type(exc).__name__}")
            print(f"  [civic.legistar] {slug} {key}: {type(exc).__name__}: "
                  f"{str(exc)[:120]}")
    if not found["meetings"] and not found["matters"]:
        found["source"] = ""
    return found


def activity(layer_key: str, person_id: int = 0, place: str = "") -> dict:
    """What the body behind this lens has actually done lately.

    Records, not judgements, and no model anywhere near them: a roll call for
    the House seat, the clerk's own calendar and legislation list for a city
    council. A lens with neither says nothing rather than filling the space
    with something that reads like a record and is not one.
    """
    empty = {"votes": [], "meetings": [], "matters": [], "source": ""}

    def build():
        try:
            if layer_key == "congress":
                return dict(empty, votes=recent_votes(person_id),
                            source="govtrack")
            if layer_key in ("council", "place") and place:
                return dict(empty, **council_agenda(_city_of(place)))
        except Exception as exc:  # noqa: BLE001 : no record is not an error
            return dict(empty, error=type(exc).__name__)
        return empty

    return _cached(f"acts:{layer_key}:{person_id}:{place.lower()}", build)


def _city_of(place: str) -> str:
    """The city out of "Oakland, California, US"."""
    return (place or "").split(",")[0].strip()


def outline_by_name(name: str) -> dict:
    """Find a boundary by name and return its geometry.

    The Census names the districts nobody can name from memory but hands back
    no shape, so the map would know you are in Congressional District 12 and
    be unable to draw it. OpenStreetMap has most of these boundaries mapped ;
    this looks one up by name and draws that.
    """
    name = " ".join((name or "").split())[:120]
    # The name goes inside an Overpass ["name"="..."] filter. A quote or a
    # backslash could end that string ; a control character could end the
    # line. Rejected rather than escaped, because the only names we care
    # about contain neither.
    if not name or any(c in name for c in ('"', "\\", "\n", "\r", "\x00")):
        return {}
    query = (f'[out:json][timeout:25];rel["boundary"]["name"="{name}"];out ids 1;')
    found = overpass(query).get("elements") or []
    if not found:
        return {}
    return outline(found[0].get("id"))


def _rings(lines: list[list]) -> list[list]:
    """Stitch a relation's way members into closed rings.

    Overpass hands back a boundary as an unordered pile of ways, which draws
    as a line and cannot be filled. Chaining them by shared endpoints turns
    the pile into polygons -- which is what lets the map shade the district
    you are inside rather than merely trace it.
    """
    pool = [list(line) for line in lines if len(line) > 1]
    rings: list[list] = []
    while pool:
        ring = pool.pop()
        joined = True
        while joined and ring[0] != ring[-1]:
            joined = False
            for i, other in enumerate(pool):
                if other[0] == ring[-1]:
                    ring += other[1:]
                elif other[-1] == ring[-1]:
                    ring += other[-2::-1]
                elif other[-1] == ring[0]:
                    ring = other[:-1] + ring
                elif other[0] == ring[0]:
                    ring = other[:0:-1] + ring
                else:
                    continue
                pool.pop(i)
                joined = True
                break
        # An unclosed chain is a boundary we only half received ; drawing it
        # as a polygon would invent an edge that is not there.
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def outline(relation_id: int) -> dict:
    """One boundary's geometry, as GeoJSON, for shading on the map.

    A MultiPolygon when the ways close into rings, and a MultiLineString when
    they do not -- a half-received boundary is drawn as the line it is rather
    than filled in as though we had all of it.
    """
    query = f"[out:json][timeout:25];rel({int(relation_id)});out geom;"
    lines = []
    # Geometry is the big response ; give each mirror longer for it.
    for element in overpass(query, OVERPASS_TIMEOUT_S + 9).get("elements") or []:
        for member in element.get("members") or []:
            if member.get("type") != "way" or member.get("role") == "inner":
                continue
            line = [[p["lon"], p["lat"]] for p in member.get("geometry") or []
                    if "lat" in p and "lon" in p]
            if len(line) > 1:
                lines.append(line)
    if not lines:
        return {}
    rings = _rings(lines)
    # Rings that close are only trustworthy as the district's shape if they
    # account for most of what we received. A boundary whose outer ring came
    # in broken but whose one-block enclave closed would otherwise be filled
    # in as the enclave -- a confident drawing of the wrong area.
    closed = sum(len(ring) for ring in rings)
    total = sum(len(line) for line in lines)
    if rings and closed * 2 > total:
        return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}
    return {"type": "MultiLineString", "coordinates": lines}


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

def _matches_collection(name: str, patterns) -> bool:
    """True when the collection name contains every word of any one pattern."""
    low = (name or "").lower()
    return any(all(word in low for word in pattern) for pattern in patterns)


def _census_layers(geographies: dict) -> dict:
    found: dict = {}
    for collection, entries in (geographies or {}).items():
        for key, patterns in _CENSUS_KEYS.items():
            if key in found or not _matches_collection(collection, patterns):
                continue
            for entry in entries or []:
                name = (entry.get("NAME") or entry.get("BASENAME") or "").strip()
                if not name:
                    continue
                found[key] = {"name": name, "detail": collection,
                              "geoid": entry.get("GEOID") or "", "source": "census"}
                break
    return found


def _osm_layers(elements: list) -> dict:
    found: dict = {}
    for element in elements or []:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        boundary = tags.get("boundary") or ""
        level = tags.get("admin_level") or ""

        # The body's own site, straight off the boundary relation. This is
        # what turns "look it up on the assessor's site" from advice into a
        # link: the county's official site is one hop from its record search,
        # and no amount of searching finds it more reliably than the map does.
        website = (tags.get("website") or tags.get("contact:website") or
                   tags.get("url") or "").strip()
        if website and not website.startswith("http"):
            website = "https://" + website

        if boundary == "political" and name:
            # Not every political boundary is a council district: OSM maps
            # congressional and state-legislative districts the same way, and
            # taking the first one meant a congressional district could sit in
            # the council slot while the real council district went missing.
            division = (tags.get("political_division") or "").lower()
            slot = ("congress" if "congress" in division else
                    "state_upper" if "senate" in division else
                    "state_lower" if ("house" in division or "assembly" in division) else
                    "council")
            if slot not in found:
                found[slot] = {"name": name, "source": "openstreetmap",
                               "detail": (division or "political district").replace("_", " "),
                               "relation": element.get("id"), "website": website}
        elif boundary == "administrative" and name:
            slot = {"8": "place"}.get(str(level))
            if slot and slot not in found:
                found[slot] = {"name": name, "source": "openstreetmap",
                               "detail": f"admin level {level}",
                               "relation": element.get("id"), "website": website}
        elif tags.get("landuse") and "landuse" not in found:
            found["landuse"] = {
                "name": str(tags["landuse"]).replace("_", " ").title(),
                "detail": "as mapped in OpenStreetMap, not the legal zoning code",
                "source": "openstreetmap",
            }
    return found


def stack(lat: float, lon: float) -> dict:
    """Every government standing over this point, closest to home first.

    Both sources are asked at once and either may fail : a layer that no
    source could name is simply absent, which is honest. The Census carries
    the districts nobody can name from memory ; OpenStreetMap carries the
    council district and the relation ids the outline needs.
    """
    key = f"{lat:.5f},{lon:.5f}"

    def build():
        results: dict = {"sources": {}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            census_future = pool.submit(census_geographies, lat, lon)
            osm_future = pool.submit(osm_areas, lat, lon)
            try:
                census = _census_layers(census_future.result())
                results["sources"]["census"] = len(census)
            except Exception as exc:  # noqa: BLE001 : one source down is not an outage
                census, results["sources"]["census"] = {}, f"{type(exc).__name__}"
            try:
                osm = _osm_layers(osm_future.result())
                results["sources"]["openstreetmap"] = len(osm)
            except Exception as exc:  # noqa: BLE001
                osm, results["sources"]["openstreetmap"] = {}, f"{type(exc).__name__}"

        layers = []
        for layer_key in LAYER_ORDER:
            spec = LAYERS[layer_key]
            # The Census is authoritative for the districts it carries ; OSM
            # fills the council district and anything the Census missed.
            found = census.get(layer_key) or osm.get(layer_key)
            if not found:
                continue
            # The Census names a body ; OpenStreetMap knows its website. Take
            # the name from whichever is authoritative and the link from
            # whichever has one.
            website = found.get("website") or (osm.get(layer_key) or {}).get("website", "")
            layers.append({
                "key": layer_key,
                "label": spec["label"],
                "color": spec["color"],
                "elected": spec["elected"],
                "decides": spec["decides"],
                "name": found["name"],
                "detail": found.get("detail", ""),
                "source": found.get("source", ""),
                "relation": found.get("relation"),
                "geoid": found.get("geoid", ""),
                "website": website,
                # What a reader should look up on that site for this layer.
                "lookup": LOOKUPS.get(layer_key, ""),
            })
        results["layers"] = layers
        return results

    return _cached(key, build)


def question_for(layer: dict, place: str) -> str:
    """The question this layer raises here, in this layer's own vocabulary."""
    spec = LAYERS.get(layer.get("key") or "", {})
    template = spec.get("asks") or "What is changing in {name} near {place}?"
    return template.format(name=layer.get("name") or spec.get("label", "this district"),
                           place=place or "this area")


def probe(lat: float = 37.8044, lon: float = -122.2712) -> dict:
    """Which of the two sources answer from this deploy's network."""
    report: dict = {}
    try:
        report["census"] = {"collections": len(census_geographies(lat, lon))}
    except Exception as exc:  # noqa: BLE001
        report["census"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        report["openstreetmap"] = {"elements": len(osm_areas(lat, lon))}
    except Exception as exc:  # noqa: BLE001
        report["openstreetmap"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return report
