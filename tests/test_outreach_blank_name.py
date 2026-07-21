"""
A whitespace-only prospect/attendee name must not crash first-name extraction.

`" ".split()[0]` raises IndexError (split() on whitespace yields []). The
template composers hardened this to `(... .strip().split() or ["there"])[0]`,
so a blank name falls back to "there" instead of 500-ing the send/preview path.
"""
from __future__ import annotations
from types import SimpleNamespace

from backend.agents import outreach as agents_outreach


def _prospect(name):
    return SimpleNamespace(
        name=name, works_on="ml infra", offers="", note="", format="dinner",
    )


def test_agents_template_blank_name_falls_back_not_crashes():
    # A whitespace-only name is the crash case (split() -> []).
    msg = agents_outreach._compose_template(
        _prospect("   "), host_bio="", framing="a founder dinner",
    )
    assert "there" in msg.note
    assert "there" in msg.message


def test_agents_template_none_name_falls_back():
    msg = agents_outreach._compose_template(
        _prospect(None), host_bio="", framing="a founder dinner",
    )
    assert "there" in msg.note


def test_normalization_expression_matches_all_sites():
    # The shared expression used at every touched site : blank -> ["there"].
    def first(name):
        return ((name or "").strip().split() or ["there"])[0]

    assert first("   ") == "there"
    assert first("") == "there"
    assert first(None) == "there"
    assert first("Ada Lovelace") == "Ada"
    assert first("  Grace  ") == "Grace"
