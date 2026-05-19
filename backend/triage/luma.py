"""
triage/luma.py : Fetch public Luma event pages and extract event metadata
without hitting the (paywalled, invite-only) Luma API.

Why this exists : operators copy-paste a lu.ma/xxx URL and the triage
Configure form auto-fills name + description + capacity + location. Saves
~30 seconds per event and means the rubric synth has real event context
to work with instead of whatever the operator remembered to type.

How it works : Luma pages are SSR'd Next.js with
  - one or more <script type="application/ld+json"> blocks containing
    schema.org Event JSON (name, description, startDate, location, etc.)
  - Open Graph meta tags as a redundant fallback
  - __NEXT_DATA__ blob with the richest data, but the shape isn't stable
    so we treat it as best-effort only.

We extract from JSON-LD first, fall back to OG tags. No HTML parser dep :
the structure is regular enough that stdlib regex is fine and avoids
adding beautifulsoup just for this.
"""
from __future__ import annotations
import json
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel


LUMA_HOSTS = {"lu.ma", "www.lu.ma", "luma.com", "www.luma.com"}

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


class LumaFetchError(Exception):
    """Public-page fetch / parse failed in a way that should surface to the
    operator as a 4xx (bad URL, page gone, etc.) rather than a 500."""


def _validate_luma_url(url: str) -> str:
    """Reject anything that isn't a lu.ma / luma.com URL so we can't be
    used as an SSRF gadget against the operator's intranet. Returns the
    normalized URL."""
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
    if host not in LUMA_HOSTS:
        raise LumaFetchError(
            f"only lu.ma / luma.com URLs are supported, got {host!r}"
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


def parse_luma_html(html: str, *, source_url: str = "") -> LumaEvent:
    """Pull event fields out of a Luma page's HTML.

    Strategy : prefer schema.org Event JSON-LD (rich + reliable); fall
    back to Open Graph tags for fields not present in JSON-LD."""
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
        name = (og.get("title") or meta.get("twitter:title") or "").strip() or None
    if not description:
        description = (
            og.get("description") or meta.get("description")
            or meta.get("twitter:description") or ""
        ).strip() or None
    if not cover_image_url:
        cover_image_url = og.get("image") or meta.get("twitter:image") or None

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
    """Fetch a public Luma event page and return parsed metadata.

    Raises LumaFetchError on bad URL / non-2xx / parse failure. Network
    timeouts are wrapped as LumaFetchError so the route can surface a
    clean 4xx instead of a generic 500."""
    safe_url = _validate_luma_url(url)
    headers = {
        # Luma serves a minimal shell to obvious bots; pretend to be a
        # normal browser so we get the SSR'd HTML with JSON-LD inline.
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
            raise LumaFetchError(f"could not reach Luma: {exc}") from exc
        if resp.status_code == 404:
            raise LumaFetchError("Luma event not found (404)")
        if resp.status_code >= 400:
            raise LumaFetchError(f"Luma responded {resp.status_code}")
        parsed = parse_luma_html(resp.text, source_url=safe_url)
        if not (parsed.name or parsed.description):
            raise LumaFetchError(
                "Luma page didn't include event metadata "
                "(is the event private or invite-only?)"
            )
        return parsed
    finally:
        if owned:
            client.close()
