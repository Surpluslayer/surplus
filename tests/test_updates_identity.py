"""Identity gate for the Exa web-search fallback (updates_watch._identity_ok).
Web search collides on same-name different people; this is the deterministic check
that the chosen result actually corroborates THIS contact (company), so we stop
attributing a stranger's news to the wrong person."""
from __future__ import annotations

from backend.agents.relationship.updates_watch import _identity_ok


def _packed(url, title, text):
    return [{"url": url, "title": title, "text": text, "published": None}]


def test_rejects_same_name_company_mismatch():
    # The real bug: contact is at Zip Security; result is about a different
    # "Vinita" at JK Lakshmi Cement. Company not corroborated -> reject.
    out = {"identity_confidence": "high", "url": "https://scanx.trade/x",
           "matched_company": "JK Lakshmi Cement"}
    packed = _packed("https://scanx.trade/x", "Vinita Singhania re-appointed MD",
                     "JK Lakshmi Cement re-appoints Vinita Singhania as MD for 5 years")
    assert _identity_ok(out, packed, "Vinita Sinha", "Zip Security") is False


def test_accepts_when_company_corroborated():
    out = {"identity_confidence": "high", "url": "https://x.com/a"}
    packed = _packed("https://x.com/a", "Aloja hits $1M",
                     "Aloja crossed $1M monthly revenue processed")
    assert _identity_ok(out, packed, "Daniel Pino", "Aloja") is True


def test_distinctive_company_token_corroborates():
    # Full company string absent, but a distinctive token ("maersk") is present.
    out = {"identity_confidence": "high", "url": "https://x.com/m"}
    packed = _packed("https://x.com/m", "Shipping giant names VP",
                     "Maersk announced a new VP of logistics today")
    assert _identity_ok(out, packed, "Alex C", "A.P. Moller - Maersk") is True


def test_rejects_low_identity_confidence():
    out = {"identity_confidence": "low", "url": "https://x.com/a"}
    packed = _packed("https://x.com/a", "Aloja $1M", "Aloja revenue milestone")
    assert _identity_ok(out, packed, "Daniel Pino", "Aloja") is False


def test_no_company_falls_back_to_name_presence():
    out = {"identity_confidence": "high", "url": "https://x.com/n"}
    hit = _packed("https://x.com/n", "Gary profiled", "Gary launched a new fund")
    assert _identity_ok(out, hit, "Gary", "") is True
    miss = _packed("https://x.com/n", "Tech week roundup", "NY tech week events listing")
    assert _identity_ok(out, miss, "Gary", "") is False


def test_generic_company_word_alone_does_not_corroborate():
    # "Security" alone (generic) must NOT corroborate "Zip Security".
    out = {"identity_confidence": "high", "url": "https://x.com/s"}
    packed = _packed("https://x.com/s", "Generic security news",
                     "A security company announced layoffs (unrelated)")
    assert _identity_ok(out, packed, "Vinita Sinha", "Zip Security") is False
