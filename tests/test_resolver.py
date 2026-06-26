"""
Unit tests for agents/resolver.py.

Focus: normalize_linkedin_url against the URL shapes we actually see from a
LinkedIn "My Code" QR payload and pasted links : no network. The text-resolve
path is gated by EXA_API_KEY and is exercised for its no-key behavior only.
"""
from __future__ import annotations

import pytest

from backend.agents.relationship import resolver


CANON = "https://www.linkedin.com/in/maya-rodriguez"


@pytest.mark.parametrize("raw,expected", [
    # The real "My Code" QR payload : profile URL + iOS share tracking params.
    ("https://www.linkedin.com/in/maya-rodriguez?utm_source=share_via"
     "&utm_content=profile&utm_medium=member_ios", CANON),
    # Android variant of the same.
    ("https://www.linkedin.com/in/maya-rodriguez?utm_source=share_via"
     "&utm_content=profile&utm_medium=member_android", CANON),
    # Already clean.
    ("https://www.linkedin.com/in/maya-rodriguez", CANON),
    # Trailing slash.
    ("https://www.linkedin.com/in/maya-rodriguez/", CANON),
    # Deeper path (contact-info deep link).
    ("https://www.linkedin.com/in/maya-rodriguez/detail/contact-info/", CANON),
    # Country subdomain canonicalizes to www.
    ("https://uk.linkedin.com/in/maya-rodriguez", CANON),
    # Scheme-less paste.
    ("www.linkedin.com/in/maya-rodriguez", CANON),
    ("linkedin.com/in/maya-rodriguez", CANON),
    # http (not https) still normalizes.
    ("http://www.linkedin.com/in/maya-rodriguez?trk=public_profile", CANON),
    # Fragment stripped.
    ("https://www.linkedin.com/in/maya-rodriguez#experience", CANON),
    # Surrounding whitespace from a sloppy paste.
    ("   https://www.linkedin.com/in/maya-rodriguez   ", CANON),
])
def test_normalize_linkedin_url_canonicalizes(raw, expected):
    assert resolver.normalize_linkedin_url(raw) == expected


def test_normalize_preserves_distinct_handles():
    assert (resolver.normalize_linkedin_url("https://www.linkedin.com/in/john-smith-123")
            == "https://www.linkedin.com/in/john-smith-123")


@pytest.mark.parametrize("raw", [
    None,
    "",
    "   ",
    "hello world",
    # LinkedIn but not a profile page.
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/company/acme",
    "https://www.linkedin.com/in/",          # empty handle
    # Look-alike host must not be trusted.
    "https://linkedin.com.evil.com/in/maya-rodriguez",
    "https://example.com/in/maya-rodriguez",
])
def test_normalize_linkedin_url_rejects_non_profiles(raw):
    assert resolver.normalize_linkedin_url(raw) is None


def test_resolve_by_text_empty_without_key(monkeypatch):
    """No EXA_API_KEY -> [] so the caller can fall back to 'type the link'."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert resolver.resolve_by_text("Maya Rodriguez", "Eng", "Acme") == []
