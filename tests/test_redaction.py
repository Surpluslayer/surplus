"""Tests for backend.redaction : the PII-minimization pass applied to LLM input."""
from __future__ import annotations

import pytest

from backend import redaction


def test_emails_redacted():
    assert redaction.scrub_pii("ping me at jane.doe@acme.co please") \
        == "ping me at [email] please"


def test_ssn_redacted():
    assert redaction.scrub_pii("SSN 123-45-6789 on file") == "SSN [ssn] on file"


@pytest.mark.parametrize("text", [
    "call +1 (415) 555-0132 tomorrow",
    "my cell is 415-555-0132",
    "reach me: 415.555.0132",
])
def test_phones_redacted(text):
    assert "[phone]" in redaction.scrub_pii(text)


@pytest.mark.parametrize("text", [
    "the 2020-2024 fiscal window",   # year range, not a phone
    "order #12345 shipped",          # short bare integer
    "we grew 300% in 2023",
])
def test_non_phone_numbers_preserved(text):
    assert redaction.scrub_pii(text) == text


def test_valid_card_redacted_invalid_preserved():
    # 4111 1111 1111 1111 is a Luhn-valid Visa test number.
    assert redaction.scrub_pii("card 4111 1111 1111 1111") == "card [card]"
    # break the Luhn checksum -> left alone (not a real card)
    assert redaction.scrub_pii("id 4111 1111 1111 1112") == "id 4111 1111 1111 1112"


def test_names_urls_topics_preserved():
    text = "Great chat with Maya Rodriguez at Stripe about https://cal.com/maya"
    assert redaction.scrub_pii(text) == text


def test_scrub_obj_recurses_and_leaves_non_strings():
    ctx = {
        "name": "Maya",
        "prior": [{"who": "them", "text": "reach me a@b.com", "when": 12345}],
        "count": 3,
        "flag": True,
    }
    out = redaction.scrub_obj(ctx)
    assert out["prior"][0]["text"] == "reach me [email]"
    assert out["prior"][0]["when"] == 12345   # non-string untouched
    assert out["name"] == "Maya" and out["count"] == 3 and out["flag"] is True


def test_disabled_is_passthrough(monkeypatch):
    monkeypatch.setenv("SURPLUS_LLM_REDACTION", "0")
    assert not redaction.enabled()
    assert redaction.scrub_pii("a@b.com 415-555-0132") == "a@b.com 415-555-0132"
    assert redaction.scrub_obj({"t": "a@b.com"}) == {"t": "a@b.com"}


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SURPLUS_LLM_REDACTION", raising=False)
    assert redaction.enabled() is True
