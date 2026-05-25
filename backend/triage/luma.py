"""
triage/luma.py : Fetch public event pages (Luma, Partiful) and extract
event metadata without hitting their (paywalled, invite-only) APIs.

Why this exists : operators copy-paste a lu.ma/xxx or partiful.com/e/xxx
URL and the triage Configure form auto-fills name + description +
capacity + location. Saves ~30 seconds per event and means the rubric
synth has real event context to work with instead of whatever the
operator remembered to type.

How it works : both Luma and Partiful pages are SSR'd Next.js with
  - one or more <script type="application/ld+json"> blocks containing
    schema.org Event JSON (name, description, startDate, location, etc.)
  - Open Graph meta tags as a redundant fallback (Partiful relies on
    these — it doesn't always emit JSON-LD Event nodes)
  - __NEXT_DATA__ blob with the richest data, but the shape isn't stable
    so we treat it as best-effort only.

We extract from JSON-LD first, fall back to OG tags. No HTML parser dep :
the structure is regular enough that stdlib regex is fine and avoids
adding beautifulsoup just for this.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from ..jsonx import extract_json


# Hosts we'll fetch on the operator's behalf. Kept to a known-good set so
# we can't be used as an SSRF gadget against the operator's intranet.
ALLOWED_EVENT_HOSTS = {
    "lu.ma", "www.lu.ma", "luma.com", "www.luma.com",
    "partiful.com", "www.partiful.com",
}

# Conservative timeout : Luma usually responds in <1s but we don't want
# to hang the request thread if their CDN hiccups.
FETCH_TIMEOUT_S = 8.0


class LumaEvent(BaseModel):
    """Parsed event fields. Everything optional so a partial page still
    returns something useful instead of 500-ing."""
    url: str
    name: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[str] = None  # ISO-8601 if present
    ends_at: Optional[str] = None
    location: Optional[str] = None
    cover_image_url: Optional[str] = None
    host_name: Optional[str] = None
    capacity: Optional[int] = None


class TriageSuggestion(BaseModel):
    """Claude-proposed defaults for fields the Luma page can't tell us
    directly (sponsor, ideal-attendee, anti-fit, etc). Operator reviews
    + edits before saving — these are starting points, not authoritative.
    Empty lists / None mean 'couldn't infer, fill in manually'."""
    sponsor_name: Optional[str] = None
    ideal_attendee_profile: Optional[str] = None
    hard_filters: list[str] = []
    anti_fit_examples: list[str] = []
    nice_to_have_signals: list[str] = []


class LumaFetchError(Exception):
    """Public-page fetch / parse failed in a way that should surface to the
    operator as a 4xx (bad URL, page gone, etc.) rather than a 500."""


def _validate_luma_url(url: str) -> str:
    """Reject anything that isn't a supported event host (Luma / Partiful)
    so we can't be used as an SSRF gadget against the operator's intranet.
    Returns the normalized URL."""
    url = (url or "").strip()
    if not url:
        raise LumaFetchError("URL is empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise LumaFetchError(f"could not parse URL: {exc}") from exc
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_EVENT_HOSTS:
        raise LumaFetchError(
            f"only lu.ma / luma.com / partiful.com URLs are supported, "
            f"got {host!r}"
        )
    return url


_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:([a-z_:]+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_NAME_OG_RE = re.compile(
    r'<meta[^>]+name=["\']([a-z:]+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


def _flatten_jsonld(blob) -> list[dict]:
    """JSON-LD can be a dict, a list, or a dict with @graph. Normalize to
    a list of dicts for easy iteration."""
    if blob is None:
        return []
    if isinstance(blob, list):
        out = []
        for item in blob:
            out.extend(_flatten_jsonld(item))
        return out
    if isinstance(blob, dict):
        if "@graph" in blob and isinstance(blob["@graph"], list):
            return _flatten_jsonld(blob["@graph"])
        return [blob]
    return []


def _pick_event(nodes: list[dict]) -> Optional[dict]:
    """Find the first schema.org Event-ish node. Luma usually emits exactly
    one Event but we tolerate noise."""
    for node in nodes:
        t = node.get("@type")
        if t == "Event" or (isinstance(t, list) and "Event" in t):
            return node
    return None


def _location_str(loc) -> Optional[str]:
    """schema.org location can be a string, a Place dict, a PostalAddress
    dict, or a list. Render to a single human-readable string."""
    if not loc:
        return None
    if isinstance(loc, str):
        return loc.strip() or None
    if isinstance(loc, list):
        for item in loc:
            s = _location_str(item)
            if s:
                return s
        return None
    if isinstance(loc, dict):
        # VirtualLocation has a url, Place has name + address
        if loc.get("@type") == "VirtualLocation":
            return loc.get("name") or loc.get("url") or "Online"
        parts = []
        if loc.get("name"):
            parts.append(loc["name"])
        addr = loc.get("address")
        if isinstance(addr, str):
            parts.append(addr)
        elif isinstance(addr, dict):
            for k in ("streetAddress", "addressLocality", "addressRegion",
                      "addressCountry"):
                v = addr.get(k)
                if v:
                    parts.append(v)
        return ", ".join(p for p in parts if p) or None
    return None


# ── Title cleanup ──────────────────────────────────────────────────────
# Partiful's og:title is decorated : "RSVP to <event name> | Partiful".
# Strip the boilerplate so the operator gets the bare event name.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[|·–—\-]\s*(?:Partiful|Luma|lu\.ma)\s*$",
    re.IGNORECASE,
)
_TITLE_PREFIX_RE = re.compile(r"^\s*RSVP\s+to\s+", re.IGNORECASE)


def _clean_event_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    s = _TITLE_SUFFIX_RE.sub("", title.strip())
    s = _TITLE_PREFIX_RE.sub("", s)
    return s.strip() or None


# ── __NEXT_DATA__ / App Router fallback (Partiful) ─────────────────────
# Partiful doesn't emit a schema.org Event JSON-LD node, so the date and
# venue don't come through JSON-LD/OG. They live in Next.js page data:
#   - Pages Router : one __NEXT_DATA__ JSON blob.
#   - App Router   : streamed as escaped JS strings in self.__next_f.push().
# Neither shape is a stable contract, so we search for plausibly-named
# keys rather than hard-coding a path — first sane hit wins.

_NEXTDATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# App Router streams RSC payload as self.__next_f.push([n, "<escaped str>"]).
_NEXT_F_RE = re.compile(
    r'self\.__next_f\.push\(\[\d+\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\)',
    re.DOTALL,
)
# Once the __next_f strings are unescaped, the event JSON appears literally;
# scan it for the same field names we look for in the blob walk.
_RAW_DATE_RE = re.compile(
    r'"(?:startDate|startsAt|startAt|startDateTime|startTime|startTimestamp|start)"'
    r'\s*:\s*(?:"([^"]+)"|(\d{10,13}))',
    re.IGNORECASE,
)
_RAW_LOC_RE = re.compile(
    r'"(?:locationName|venueName|venue|formattedAddress|geoAddress|placeName|address)"'
    r'\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# Exact (lowercased) key names, not substrings : substring matching on
# "start"/"location" pulls in unrelated fields (subscription starts,
# locationPrivacy enums, etc) and yields garbage.
_DATE_KEYS = {"startdate", "startsat", "starttime", "startdatetime",
              "start", "startts", "starttimestamp"}
_LOCATION_KEYS = {"location", "locationname", "locationinfo", "venue",
                  "venuename", "address", "formattedaddress", "geoaddress",
                  "placename", "place"}


def _coerce_iso_date(val) -> Optional[str]:
    """Best-effort : turn a date-ish value into something starting with
    YYYY-MM-DD (the frontend slices the first 10 chars). Handles ISO
    strings, epoch seconds/millis, and Firestore-style {seconds} dicts."""
    if isinstance(val, bool):
        return None
    if isinstance(val, str):
        s = val.strip()
        return s if re.match(r"\d{4}-\d{2}-\d{2}", s) else None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        if ts < 1e8 or ts > 4e9:  # outside ~1973–2096 : not a sane event ts
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(val, dict):
        for k in ("seconds", "_seconds"):
            sec = val.get(k)
            if isinstance(sec, (int, float)) and not isinstance(sec, bool):
                return _coerce_iso_date(sec)
    return None


def _clean_location_value(val) -> Optional[str]:
    """Render a location-keyed value to a string, rejecting enum-like
    junk ("PUBLIC", "HIDDEN") that some blobs stash under a location key."""
    s = _location_str(val)
    if not s:
        return None
    if " " not in s and "," not in s and s.isupper():
        return None
    return s


def _scan_nextdata(node, state: dict) -> None:
    """Depth-first search for a start date + location. Mutates state
    ({"date": ..., "loc": ...}); stops once both are filled."""
    if state["date"] and state["loc"]:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if state["date"] is None and kl in _DATE_KEYS:
                state["date"] = _coerce_iso_date(v)
            if state["loc"] is None and kl in _LOCATION_KEYS:
                state["loc"] = _clean_location_value(v)
            if state["date"] and state["loc"]:
                return
            if isinstance(v, (dict, list)):
                _scan_nextdata(v, state)
                if state["date"] and state["loc"]:
                    return
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _scan_nextdata(item, state)
                if state["date"] and state["loc"]:
                    return


def _decode_next_f(html: str) -> str:
    """Concatenate the unescaped self.__next_f.push() payload chunks (App
    Router). Each chunk is a JSON string literal, so json.loads unescapes
    it; joining yields the RSC stream with event JSON inline."""
    chunks: list[str] = []
    for m in _NEXT_F_RE.finditer(html):
        try:
            chunks.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def _scan_text_for_event(text: str) -> tuple[Optional[str], Optional[str]]:
    """Regex-scan a flat text payload (decoded __next_f) for a start date
    and location. Used when there's no clean JSON object to walk."""
    date = None
    m = _RAW_DATE_RE.search(text)
    if m:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        date = _coerce_iso_date(int(raw) if (raw and raw.isdigit()) else raw)
    loc = None
    m = _RAW_LOC_RE.search(text)
    if m:
        loc = _clean_location_value(m.group(1))
    return date, loc


def _parse_nextdata(html: str) -> tuple[Optional[str], Optional[str]]:
    """Pull (starts_at, location) out of Next.js page data — the Pages
    Router __NEXT_DATA__ blob if present, else the App Router stream."""
    for m in _NEXTDATA_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        state = {"date": None, "loc": None}
        _scan_nextdata(data, state)
        if state["date"] or state["loc"]:
            return state["date"], state["loc"]
    payload = _decode_next_f(html)
    if payload:
        return _scan_text_for_event(payload)
    return None, None


def parse_luma_html(html: str, *, source_url: str = "") -> LumaEvent:
    """Pull event fields out of an event page's HTML (Luma / Partiful).

    Strategy : prefer schema.org Event JSON-LD (rich + reliable); fall
    back to Open Graph tags for fields not present in JSON-LD. Partiful
    leans on the OG path since it doesn't always emit a JSON-LD Event."""
    name: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    location: Optional[str] = None
    cover_image_url: Optional[str] = None
    host_name: Optional[str] = None
    capacity: Optional[int] = None

    for match in _JSONLD_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event = _pick_event(_flatten_jsonld(data))
        if not event:
            continue
        name = name or (event.get("name") or "").strip() or None
        description = description or (event.get("description") or "").strip() or None
        starts_at = starts_at or event.get("startDate")
        ends_at = ends_at or event.get("endDate")
        location = location or _location_str(event.get("location"))
        # image can be a string or list
        img = event.get("image")
        if isinstance(img, list) and img:
            img = img[0]
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, str) and not cover_image_url:
            cover_image_url = img
        organizer = event.get("organizer")
        if isinstance(organizer, list) and organizer:
            organizer = organizer[0]
        if isinstance(organizer, dict):
            host_name = host_name or organizer.get("name")
        elif isinstance(organizer, str):
            host_name = host_name or organizer
        # schema.org "maximumAttendeeCapacity" or remainingAttendeeCapacity
        cap = event.get("maximumAttendeeCapacity")
        if isinstance(cap, (int, float)) and capacity is None:
            capacity = int(cap)

    # OG fallback : every Luma page has these even when JSON-LD is missing.
    og = {m.group(1).lower(): m.group(2) for m in _OG_RE.finditer(html)}
    meta = {m.group(1).lower(): m.group(2) for m in _NAME_OG_RE.finditer(html)}
    if not name:
        name = _clean_event_title(og.get("title") or meta.get("twitter:title"))
    if not description:
        description = (
            og.get("description") or meta.get("description")
            or meta.get("twitter:description") or ""
        ).strip() or None
    if not cover_image_url:
        cover_image_url = og.get("image") or meta.get("twitter:image") or None

    # Next.js page-data fallback : Partiful carries date + venue here
    # rather than in JSON-LD/OG. Only fills fields JSON-LD didn't provide.
    if not (starts_at and location):
        nd_date, nd_loc = _parse_nextdata(html)
        starts_at = starts_at or nd_date
        location = location or nd_loc

    return LumaEvent(
        url=source_url,
        name=name,
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        cover_image_url=cover_image_url,
        host_name=host_name,
        capacity=capacity,
    )


def fetch_luma_event(url: str, *, client: Optional[httpx.Client] = None) -> LumaEvent:
    """Fetch a public event page (Luma / Partiful) and return parsed metadata.

    Raises LumaFetchError on bad URL / non-2xx / parse failure. Network
    timeouts are wrapped as LumaFetchError so the route can surface a
    clean 4xx instead of a generic 500."""
    safe_url = _validate_luma_url(url)
    headers = {
        # These hosts serve a minimal shell to obvious bots; pretend to be
        # a normal browser so we get the SSR'd HTML with JSON-LD / OG inline.
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
        ),
        "accept": "text/html,application/xhtml+xml",
    }
    owned = False
    if client is None:
        client = httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True)
        owned = True
    try:
        try:
            resp = client.get(safe_url, headers=headers)
        except httpx.HTTPError as exc:
            raise LumaFetchError(f"could not reach the event page: {exc}") from exc
        if resp.status_code == 404:
            raise LumaFetchError("event not found (404)")
        if resp.status_code >= 400:
            raise LumaFetchError(f"event page responded {resp.status_code}")
        parsed = parse_luma_html(resp.text, source_url=safe_url)
        if not (parsed.name or parsed.description):
            raise LumaFetchError(
                "event page didn't include metadata "
                "(is the event private or invite-only?)"
            )
        return parsed
    finally:
        if owned:
            client.close()


# ── Claude-inferred triage suggestions ─────────────────────────────────

_SUGGEST_MODEL = "claude-haiku-4-5-20251001"
_SUGGEST_MAX_TOKENS = 800
_SUGGEST_TIMEOUT_S = 20

_SUGGEST_SYSTEM = """You read a public event description and propose triage \
criteria the event operator can use to filter applicants. You return JSON only.

Rules:
- Be conservative. If the description is too vague to infer a field, return \
null (for scalars) or an empty list. Don't fabricate.
- ideal_attendee_profile : 1–2 sentences naming the kind of person the event \
is actually for, based on what the description says (not what would be nice).
- hard_filters : disqualifying conditions implied by the description (location \
requirement, application gate, etc). One short clause each.
- anti_fit_examples : categories of applicant the event is clearly NOT for, \
based on phrasing in the description. One short clause each.
- nice_to_have_signals : positive indicators the event explicitly values \
(stage, traction, role, etc). One short clause each.
- sponsor_name : the brand(s) hosting / co-hosting if obvious from the title \
or description; null otherwise.

Schema:
{
  "sponsor_name": string | null,
  "ideal_attendee_profile": string | null,
  "hard_filters": string[],
  "anti_fit_examples": string[],
  "nice_to_have_signals": string[]
}"""


def suggest_triage_config(event: LumaEvent) -> TriageSuggestion:
    """Feed the Luma description into Haiku and return proposed triage
    criteria. Best-effort : on API failure / bad JSON / missing key, returns
    an empty TriageSuggestion so the operator still gets the import and can
    fill the fields manually."""
    if not event.description:
        return TriageSuggestion()
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return TriageSuggestion()

    parts = []
    if event.name:
        parts.append(f"Event title: {event.name}")
    if event.host_name:
        parts.append(f"Host: {event.host_name}")
    if event.location:
        parts.append(f"Location: {event.location}")
    parts.append("")
    parts.append("Description:")
    parts.append(event.description)
    user_msg = "\n".join(parts)

    try:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=_SUGGEST_MODEL,
            max_tokens=_SUGGEST_MAX_TOKENS,
            timeout=_SUGGEST_TIMEOUT_S,
            system=_SUGGEST_SYSTEM,
            messages=[
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [triage.luma.suggest] Haiku failed: "
              f"{type(exc).__name__}: {exc}")
        return TriageSuggestion()

    # Claude 4.x dropped assistant-message prefill; extract_json scans for
    # the first JSON object in the model's natural output.
    text_chunks = [b.text for b in resp.content
                   if getattr(b, "type", "") == "text"]
    full = "\n".join(text_chunks)
    parsed = extract_json(full) or {}

    def _str_list(v) -> list[str]:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    sponsor = parsed.get("sponsor_name")
    ideal = parsed.get("ideal_attendee_profile")
    return TriageSuggestion(
        sponsor_name=(sponsor.strip() if isinstance(sponsor, str)
                      and sponsor.strip() else None),
        ideal_attendee_profile=(ideal.strip() if isinstance(ideal, str)
                                and ideal.strip() else None),
        hard_filters=_str_list(parsed.get("hard_filters")),
        anti_fit_examples=_str_list(parsed.get("anti_fit_examples")),
        nice_to_have_signals=_str_list(parsed.get("nice_to_have_signals")),
    )
