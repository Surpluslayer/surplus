"""
Tests for backend.triage.luma : Luma event-page scraping.

We avoid hitting the network by feeding parse_luma_html() canned HTML
fragments that mirror the SSR'd shape Luma actually emits (JSON-LD +
Open Graph). The validate / fetch wrapper is exercised separately with
a stub httpx.Client so SSRF guard + error mapping are covered too.
"""
from __future__ import annotations
import json

import pytest

from backend.triage.luma import (
    LumaEvent,
    LumaFetchError,
    TriageSuggestion,
    fetch_luma_event,
    parse_luma_html,
    suggest_triage_config,
    _validate_luma_url,
)


# ── URL validation ────────────────────────────────────────────────────

def test_validate_accepts_lu_ma():
    assert _validate_luma_url("https://lu.ma/abc123").startswith("https://lu.ma/")


def test_validate_accepts_luma_com():
    assert _validate_luma_url("https://luma.com/foo") == "https://luma.com/foo"


def test_validate_adds_scheme():
    assert _validate_luma_url("lu.ma/xyz").startswith("https://lu.ma/")


def test_validate_rejects_non_luma_host():
    """SSRF guard : we shouldn't fetch arbitrary URLs on the operator's behalf."""
    with pytest.raises(LumaFetchError):
        _validate_luma_url("https://evil.example.com/lu.ma")


def test_validate_rejects_empty():
    with pytest.raises(LumaFetchError):
        _validate_luma_url("")


# ── HTML parsing ──────────────────────────────────────────────────────

def _page_with_jsonld(event_obj: dict, extras: str = "") -> str:
    """Render a minimal Luma-shaped HTML page with one JSON-LD Event block."""
    return f"""<html><head>
<script type="application/ld+json">{json.dumps(event_obj)}</script>
{extras}
</head><body></body></html>"""


def test_parse_extracts_jsonld_event_fields():
    html = _page_with_jsonld({
        "@context": "https://schema.org",
        "@type": "Event",
        "name": "Stripe x ElevenLabs Cafe",
        "description": "Builders shipping consumer AI products.",
        "startDate": "2026-06-01T18:00:00-07:00",
        "endDate": "2026-06-01T21:00:00-07:00",
        "maximumAttendeeCapacity": 40,
        "location": {
            "@type": "Place", "name": "Stripe HQ",
            "address": {"streetAddress": "510 Townsend St",
                        "addressLocality": "San Francisco"},
        },
        "image": "https://images.lu.ma/cover.jpg",
        "organizer": {"@type": "Organization", "name": "abundant.ai"},
    })
    ev = parse_luma_html(html, source_url="https://lu.ma/test")
    assert ev.name == "Stripe x ElevenLabs Cafe"
    assert "consumer AI" in (ev.description or "")
    assert ev.starts_at == "2026-06-01T18:00:00-07:00"
    assert ev.ends_at == "2026-06-01T21:00:00-07:00"
    assert ev.capacity == 40
    assert "Stripe HQ" in (ev.location or "")
    assert "Townsend" in (ev.location or "")
    assert ev.cover_image_url == "https://images.lu.ma/cover.jpg"
    assert ev.host_name == "abundant.ai"


def test_parse_falls_back_to_og_when_jsonld_missing():
    """No JSON-LD block, only OG tags : OG fallback should populate name + desc."""
    html = """<html><head>
<meta property="og:title" content="Founders Dinner NYC" />
<meta property="og:description" content="20 seats. Pre-seed to Series A." />
<meta property="og:image" content="https://cdn.lu.ma/og.png" />
</head></html>"""
    ev = parse_luma_html(html, source_url="https://lu.ma/og-only")
    assert ev.name == "Founders Dinner NYC"
    assert "20 seats" in (ev.description or "")
    assert ev.cover_image_url == "https://cdn.lu.ma/og.png"


def test_parse_handles_jsonld_graph_envelope():
    """JSON-LD often wraps multiple nodes in @graph; we should still find Event."""
    html = _page_with_jsonld({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Luma"},
            {"@type": "Event", "name": "Graph event",
             "description": "test"},
        ],
    })
    ev = parse_luma_html(html)
    assert ev.name == "Graph event"


def test_parse_handles_virtual_location():
    html = _page_with_jsonld({
        "@type": "Event",
        "name": "Online thing",
        "description": "zoom",
        "location": {"@type": "VirtualLocation",
                     "url": "https://zoom.us/abc"},
    })
    ev = parse_luma_html(html)
    assert ev.location == "https://zoom.us/abc"


def test_parse_tolerates_bad_jsonld_block():
    """A malformed JSON-LD <script> shouldn't crash the parser; OG should still
    win as a fallback."""
    html = """<html><head>
<script type="application/ld+json">{not json oops</script>
<meta property="og:title" content="Fallback title" />
<meta property="og:description" content="Fallback desc" />
</head></html>"""
    ev = parse_luma_html(html)
    assert ev.name == "Fallback title"
    assert ev.description == "Fallback desc"


# ── fetch_luma_event with stub client ────────────────────────────────

class _StubResponse:
    def __init__(self, *, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _StubClient:
    def __init__(self, response: _StubResponse):
        self._resp = response
        self.last_url = None

    def get(self, url, headers=None):
        self.last_url = url
        return self._resp

    def close(self):
        pass


def test_fetch_returns_parsed_event():
    html = _page_with_jsonld({
        "@type": "Event", "name": "Stub event",
        "description": "via stub client",
    })
    client = _StubClient(_StubResponse(status_code=200, text=html))
    ev = fetch_luma_event("https://lu.ma/stub", client=client)
    assert ev.name == "Stub event"
    assert client.last_url == "https://lu.ma/stub"


def test_fetch_raises_on_404():
    client = _StubClient(_StubResponse(status_code=404, text=""))
    with pytest.raises(LumaFetchError):
        fetch_luma_event("https://lu.ma/missing", client=client)


def test_fetch_raises_when_no_metadata():
    """Private / draft Luma pages return a stub HTML with no event JSON;
    we should surface that as a 4xx-friendly error, not silently return
    an empty record."""
    client = _StubClient(_StubResponse(
        status_code=200, text="<html><head></head><body>nope</body></html>"))
    with pytest.raises(LumaFetchError):
        fetch_luma_event("https://lu.ma/private", client=client)


# ── Claude-suggestion fallback ────────────────────────────────────────

def test_suggest_returns_empty_without_description():
    """No description -> no Claude call -> empty TriageSuggestion."""
    ev = LumaEvent(url="https://lu.ma/x", name="No desc")
    sug = suggest_triage_config(ev)
    assert sug == TriageSuggestion()


def test_suggest_returns_empty_without_api_key(monkeypatch):
    """No ANTHROPIC_API_KEY -> degrade gracefully to empty suggestions
    so the import still works in dev / when the key rotates."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ev = LumaEvent(url="https://lu.ma/x", name="X",
                   description="A founders dinner in NYC.")
    sug = suggest_triage_config(ev)
    assert sug == TriageSuggestion()
