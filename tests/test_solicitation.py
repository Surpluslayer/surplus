"""Tests for the Rule 7.3 gate (backend/solicitation.py).

Pure functions, no DB -- these assert the decision table, not delivery.
JURISDICTION_RULES values are illustrative (see module docstring); these
tests pin the *logic*, not the *legal correctness* of any state's numbers.
"""
from __future__ import annotations

from datetime import date

from backend.solicitation import (
    RelationshipType,
    SolicitationContext,
    evaluate,
)


def test_existing_client_is_always_exempt():
    ctx = SolicitationContext(
        jurisdiction="FL",
        channel="phone_call",  # would otherwise be barred outright
        relationship_type=RelationshipType.EXISTING_CLIENT,
    )
    verdict = evaluate(ctx)
    assert verdict.allowed
    assert "exempt" in verdict.reason


def test_realtime_channel_barred_for_cold_prospect():
    ctx = SolicitationContext(
        jurisdiction="NY",
        channel="phone_call",
        relationship_type=RelationshipType.PROSPECT,
    )
    verdict = evaluate(ctx)
    assert not verdict.allowed
    assert "real-time" in verdict.reason


def test_written_channel_permitted_with_disclosure():
    ctx = SolicitationContext(
        jurisdiction="NY",
        channel="linkedin_dm",
        relationship_type=RelationshipType.PROSPECT,
    )
    verdict = evaluate(ctx)
    assert verdict.allowed
    assert verdict.requires_disclosure_label
    assert verdict.disclosure_text == "Attorney Advertising"


def test_sensitive_matter_within_cooldown_is_blocked():
    ctx = SolicitationContext(
        jurisdiction="FL",
        channel="email",
        relationship_type=RelationshipType.PROSPECT,
        matter_is_sensitive=True,
        triggering_event_date=date(2026, 8, 1),
        today=date(2026, 8, 11),  # 10 days elapsed, FL cooldown is 30
    )
    verdict = evaluate(ctx)
    assert not verdict.allowed
    assert "cooldown" in verdict.reason


def test_sensitive_matter_after_cooldown_is_permitted():
    ctx = SolicitationContext(
        jurisdiction="FL",
        channel="email",
        relationship_type=RelationshipType.PROSPECT,
        matter_is_sensitive=True,
        triggering_event_date=date(2026, 1, 1),
        today=date(2026, 8, 11),
    )
    verdict = evaluate(ctx)
    assert verdict.allowed


def test_volume_cap_blocks_repeat_solicitation():
    ctx = SolicitationContext(
        jurisdiction="FL",
        channel="email",
        relationship_type=RelationshipType.PROSPECT,
        recent_solicitation_count_in_window=1,  # FL cap is 1 per 30d
    )
    verdict = evaluate(ctx)
    assert not verdict.allowed
    assert "caps solicitation" in verdict.reason


def test_unknown_jurisdiction_defaults_to_ny():
    ctx = SolicitationContext(
        jurisdiction="ZZ",  # not in JURISDICTION_RULES -- falls through to NY
        channel="phone_call",
        relationship_type=RelationshipType.PROSPECT,
    )
    verdict = evaluate(ctx)
    assert not verdict.allowed  # real-time is barred under NY's rule too


def test_unknown_jurisdiction_written_channel_still_requires_disclosure():
    ctx = SolicitationContext(
        jurisdiction="ZZ",
        channel="email",
        relationship_type=RelationshipType.PROSPECT,
    )
    verdict = evaluate(ctx)
    assert verdict.allowed
    assert verdict.requires_disclosure_label
