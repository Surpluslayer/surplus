"""Tests for the two-tab ask router (book vs referral) in routes/book.py.

The router is deterministic (reuses detect_network_intent, never mutates it), so
these lock its behavior: which tab a query wants, when the client auto-switches,
and -- critically -- that genuinely ambiguous asks STAY on the current tab and
only get a soft cross-hint.
"""
from backend.routes.book import (
    _ask_signal, _natural_ask_mode, _routed_to, _route_response,
)
from backend.agents.relationship.pipeline.context.network_search import (
    detect_network_intent,
)


# ── strong signals ────────────────────────────────────────────────────────────

# Explicit referral (the original detect_network_intent set).
EXPLICIT_REFERRAL = [
    "Who do I know at Stripe?",
    "who in my 2nd degree works at a company I'm targeting?",
    "2nd degree founders in NYC",
    "can someone introduce me to the Ramp team?",
    "who can refer me into Figma",
]

# Bucket E/F: reach-through verbs the shared gate MISSED -> must now be referral.
REACH_THROUGH_REFERRAL = [
    "I want to meet the Ramp founders",
    "get me in front of a VC",
    "help me connect with someone at Notion",
    "I need to reach the CFO of Acme",
    "connect me with a designer",
    "put me in touch with a recruiter",
    "introduce me to someone at OpenAI",
]

# Explicit book: existing-relationship verbs.
EXPLICIT_BOOK = [
    "who should I reach out to this week?",
    "reach out to my sales prospects",
    "who's gone quiet lately",
    "draft a note to Sarah congratulating her new role",
    "what's new with my contacts",
    "who should I follow up with",
    "remind me who I last spoke to at Acme",
]

# Genuinely ambiguous: target with no direction verb. Same words, both readings.
AMBIGUOUS = [
    "anyone at OpenAI I should talk to?",
    "people at Stripe",
    "who at Anthropic",
    "contacts at Google",
    "find me people in crypto",
    "who works in fintech",
    "investors in AI",
    "who's hiring right now",
    "who's raising",
]


def test_explicit_referral_signal():
    for q in EXPLICIT_REFERRAL:
        assert _ask_signal(q) == "referral", f"want referral: {q!r}"


def test_reach_through_now_referral():
    # These were the real misroutes (fell to book before). Now referral.
    for q in REACH_THROUGH_REFERRAL:
        assert _ask_signal(q) == "referral", f"want referral: {q!r}"


def test_explicit_book_signal():
    for q in EXPLICIT_BOOK:
        assert _ask_signal(q) == "book", f"want book: {q!r}"


def test_ambiguous_signal():
    for q in AMBIGUOUS:
        assert _ask_signal(q) == "ambiguous", f"want ambiguous: {q!r}"


# ── routing decisions (auto-switch vs stay + cross-hint) ───────────────────────

def test_referral_ask_in_today_autoswitches():
    for q in EXPLICIT_REFERRAL + REACH_THROUGH_REFERRAL:
        routed, hint = _route_response("book", q)
        assert routed == "referral", f"should switch to referral: {q!r}"
        assert hint is None


def test_book_ask_in_referrals_autoswitches():
    for q in EXPLICIT_BOOK:
        routed, hint = _route_response("referral", q)
        assert routed == "book", f"should switch to book: {q!r}"
        assert hint is None


def test_ambiguous_stays_put_with_cross_hint():
    for q in AMBIGUOUS:
        # In Today -> stay, hint Referrals.
        routed, hint = _route_response("book", q)
        assert routed is None, f"must NOT switch on ambiguous: {q!r}"
        assert hint == "referral", f"want cross-hint referral: {q!r}"
        # In Referrals -> stay, hint Today. (The old binary bug yanked these out.)
        routed, hint = _route_response("referral", q)
        assert routed is None, f"must NOT switch on ambiguous: {q!r}"
        assert hint == "book", f"want cross-hint book: {q!r}"


def test_matching_tab_stays_no_hint():
    routed, hint = _route_response("referral", "who do I know at Stripe?")
    assert routed is None and hint is None
    routed, hint = _route_response("book", "who should I reach out to")
    assert routed is None and hint is None


def test_legacy_caller_never_routed():
    # No declared tab -> no routing, no hint. Behavior unchanged for old clients.
    for req in (None, "", "garbage"):
        assert _route_response(req, "who do I know at Stripe?") == (None, None)


# ── regression guards: nothing existing is broken ──────────────────────────────

def test_detect_network_intent_unchanged():
    # The shared gate MUST still fire on the explicit referral set (we never
    # touched it) and MUST NOT fire on explicit book asks.
    for q in EXPLICIT_REFERRAL:
        assert detect_network_intent(q) is True, f"gate regressed: {q!r}"
    for q in EXPLICIT_BOOK:
        assert detect_network_intent(q) is False, f"gate over-fires: {q!r}"


def test_reach_out_never_leaks_to_referral():
    # The one dangerous overlap: "reach out to" is book, "reach the X" is
    # referral. Prove the book verb is never misread as reach-through.
    for q in ["reach out to my prospects", "who should I reach out to",
              "let's reach out to the whole list"]:
        assert _ask_signal(q) == "book", f"reach-out leaked: {q!r}"


def test_natural_ask_mode_binary_backcompat():
    # _natural_ask_mode still returns only book|referral (ambiguous -> book).
    for q in AMBIGUOUS + EXPLICIT_BOOK:
        assert _natural_ask_mode(q) == "book"
    for q in EXPLICIT_REFERRAL:
        assert _natural_ask_mode(q) == "referral"


def test_routed_to_pure_mismatch_helper():
    # The low-level helper still behaves (used by _route_response for strong sigs).
    assert _routed_to("book", "referral") == "referral"
    assert _routed_to("referral", "book") == "book"
    assert _routed_to("book", "book") is None
    assert _routed_to(None, "referral") is None
