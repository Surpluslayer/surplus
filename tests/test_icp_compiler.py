"""
Tests for backend/triage/icp_compiler.py : the deterministic ICP -> triage_config
compiler (the "rules layer").

These pin down the policy contract the compiler must satisfy against its real
consumers (recommend.apply_archetype_priority / Thresholds.from_dict and
consolidate._auto_accept_ok). If the bands or the archetype_priority shape drift,
these break.
"""
from __future__ import annotations

import json
import os

import pytest

from backend.triage.icp_compiler import compile_icp
from backend.triage import recommend


# --------------------------------------------------------------------------- #
# 1. Threshold band selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fmt,accept,maybe,reject",
    [
        ("Casual mixer", 65, 50, 35),
        ("coffee at a café", 65, 50, 35),
        ("open coworking hangout", 65, 50, 35),
        ("Standard sponsored dinner", 72, 55, 40),
        ("evening reception", 72, 55, 40),
        ("Exclusive invite-only dinner", 78, 60, 45),
        ("intimate small fireside", 78, 60, 45),
        ("", 72, 55, 40),  # unknown/blank -> standard dinner band
        ("some weird unrecognized format", 72, 55, 40),
    ],
)
def test_threshold_band_selection(fmt, accept, maybe, reject):
    cfg = compile_icp({"format": fmt})
    t = cfg["thresholds"]
    assert t["accept_fit_min"] == accept
    assert t["maybe_fit_min"] == maybe
    assert t["reject_fit_max"] == reject
    # Confidence mins are always uniform.
    assert t["accept_confidence_min"] == 60
    assert t["maybe_confidence_min"] == 45


def test_thresholds_parse_into_recommend_Thresholds():
    """The emitted thresholds must round-trip through the real consumer."""
    cfg = compile_icp({"format": "intimate dinner"})
    t = recommend.Thresholds.from_dict(cfg["thresholds"])
    assert t.accept_fit_min == 78
    assert t.maybe_fit_min == 60
    assert t.reject_fit_max == 45
    assert t.accept_confidence_min == 60
    assert t.maybe_confidence_min == 45


def test_exclusive_hint_beats_mixer_hint():
    # "small mixer" contains both hints; exclusive must win (more selective).
    cfg = compile_icp({"format": "small intimate mixer"})
    assert cfg["thresholds"]["accept_fit_min"] == 78


# --------------------------------------------------------------------------- #
# 2. archetype_priority: boost / cap / clamping / auto_accept
# --------------------------------------------------------------------------- #
def test_priority_archetype_becomes_boost():
    cfg = compile_icp({"priority_archetypes": ["founder"]})
    ap = cfg["archetype_priority"]
    assert ap["boost"] == {"founder": 12}
    assert ap["cap"] == {}


def test_deprioritize_archetype_becomes_cap():
    cfg = compile_icp({"deprioritize_archetypes": ["investor"]})
    ap = cfg["archetype_priority"]
    assert ap["cap"] == {"investor": 68}
    assert ap["boost"] == {}


def test_boost_and_cap_within_consumer_ranges():
    cfg = compile_icp(
        {"priority_archetypes": ["founder"], "deprioritize_archetypes": ["investor"]}
    )
    ap = cfg["archetype_priority"]
    # Boost clamped to [0, 25], cap clamped to [40, 90].
    assert 0 <= ap["boost"]["founder"] <= 25
    assert 40 <= ap["cap"]["investor"] <= 90


def test_require_corroboration_for_boost_default_true():
    cfg = compile_icp({"priority_archetypes": ["founder"]})
    assert cfg["archetype_priority"]["require_corroboration_for_boost"] is True


def test_require_corroboration_for_boost_can_be_false():
    cfg = compile_icp(
        {"priority_archetypes": ["founder"], "require_corroboration": False}
    )
    ap = cfg["archetype_priority"]
    assert ap["require_corroboration_for_boost"] is False
    assert ap["auto_accept"]["require_corroboration"] is False


def test_auto_accept_only_when_priority_archetype_exists():
    no_priority = compile_icp({"deprioritize_archetypes": ["investor"]})
    assert "auto_accept" not in no_priority["archetype_priority"]

    with_priority = compile_icp({"priority_archetypes": ["founder"]})
    auto = with_priority["archetype_priority"]["auto_accept"]
    assert auto["archetype"] == "founder"
    assert auto["require_corroboration"] is True
    assert auto["min_dimension"] == {"company_relevance": 55}


@pytest.mark.skipif(
    not hasattr(recommend, "apply_archetype_priority"),
    reason="apply_archetype_priority lands in the concurrent recommend.py edit",
)
def test_archetype_priority_consumed_by_recommend():
    """apply_archetype_priority must accept the emitted policy and behave."""
    cfg = compile_icp(
        {"priority_archetypes": ["founder"], "deprioritize_archetypes": ["investor"]}
    )
    policy = cfg["archetype_priority"]

    # Corroborated founder gets the boost.
    fit, reasons = recommend.apply_archetype_priority(
        60, "founder", founder_corroborated=True, policy=policy
    )
    assert fit == 72
    assert any("boost" in r for r in reasons)

    # Investor gets capped to 68.
    fit, reasons = recommend.apply_archetype_priority(
        90, "investor", policy=policy
    )
    assert fit == 68


# --------------------------------------------------------------------------- #
# 3. Sparse / malformed ICP never raises
# --------------------------------------------------------------------------- #
def test_empty_icp_returns_valid_config():
    cfg = compile_icp({})
    assert cfg["event_goal"] == ""
    assert cfg["hard_filters"] == []
    assert cfg["anti_fit_examples"] == []
    assert cfg["capacity"] == 0
    ap = cfg["archetype_priority"]
    assert ap["boost"] == {} and ap["cap"] == {}
    assert "auto_accept" not in ap
    # Unknown format -> standard band.
    assert cfg["thresholds"]["accept_fit_min"] == 72


def test_none_and_junk_icp_do_not_raise():
    assert compile_icp(None)["thresholds"]["accept_fit_min"] == 72
    assert compile_icp("not a dict")["capacity"] == 0  # type: ignore[arg-type]


def test_format_only_icp():
    cfg = compile_icp({"format": "dinner"})
    assert cfg["thresholds"]["accept_fit_min"] == 72


def test_malformed_field_types_coerced_safely():
    cfg = compile_icp(
        {
            "capacity": "not-an-int",
            "priority_archetypes": "founder",  # bare string, not list
            "anti_fit": ["", "  ", "recruiters"],  # blanks dropped
            "nice_to_have": 12345,  # junk
        }
    )
    assert cfg["capacity"] == 0
    assert cfg["archetype_priority"]["boost"] == {"founder": 12}
    assert cfg["anti_fit_examples"] == ["recruiters"]
    assert isinstance(cfg["nice_to_have_signals"], list)


def test_negative_capacity_clamped_with_warning():
    cfg = compile_icp({"capacity": -5})
    assert cfg["capacity"] == 0
    assert any("capacity" in w for w in cfg.get("_compiler_warnings", []))


# --------------------------------------------------------------------------- #
# 4. city -> hard_filter (only for non-remote formats)
# --------------------------------------------------------------------------- #
def test_city_seeds_hard_filter_for_in_person():
    cfg = compile_icp({"city": "NYC", "format": "intimate dinner"})
    assert "Must be based in NYC" in cfg["hard_filters"]


@pytest.mark.parametrize("fmt", ["remote fireside", "virtual mixer", "online panel"])
def test_remote_format_no_location_hard_filter(fmt):
    cfg = compile_icp({"city": "NYC", "format": fmt})
    assert cfg["hard_filters"] == []
    # But a soft city signal is still derived.
    assert any("NYC" in s for s in cfg["nice_to_have_signals"])


def test_no_city_no_hard_filter():
    cfg = compile_icp({"format": "dinner"})
    assert cfg["hard_filters"] == []


# --------------------------------------------------------------------------- #
# 5. Conflict resolution: priority wins, warning emitted
# --------------------------------------------------------------------------- #
def test_conflict_priority_wins_over_deprioritize():
    cfg = compile_icp(
        {
            "priority_archetypes": ["founder"],
            "deprioritize_archetypes": ["founder", "investor"],
        }
    )
    ap = cfg["archetype_priority"]
    assert ap["boost"] == {"founder": 12}
    # founder dropped from cap; investor kept.
    assert "founder" not in ap["cap"]
    assert ap["cap"] == {"investor": 68}
    warnings = cfg.get("_compiler_warnings", [])
    assert any("founder" in w and "priority wins" in w for w in warnings)


def test_no_warnings_key_when_clean():
    cfg = compile_icp({"priority_archetypes": ["founder"]})
    assert "_compiler_warnings" not in cfg


# --------------------------------------------------------------------------- #
# 6. Round-trip-ish vs the hand-authored Bryan Kim config
# --------------------------------------------------------------------------- #
# The hand-authored archetype_priority we must reproduce semantically. Mirrored
# inline so the test is self-contained even if icp_bryankim.json isn't present
# in this worktree (it lives at the surplus repo root).
_BRYAN_KIM_ARCHETYPE_PRIORITY = {
    "boost": {"founder": 12},
    "cap": {"investor": 68},
    "require_corroboration_for_boost": True,
    "auto_accept": {
        "archetype": "founder",
        "require_corroboration": True,
        "min_dimension": {"company_relevance": 55},
    },
}


def _load_reference_archetype_priority():
    """Prefer the real icp_bryankim.json if reachable; else use the mirror."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, os.pardir, "icp_bryankim.json")
    if os.path.exists(candidate):
        with open(candidate) as fh:
            data = json.load(fh)
        return data["triage_config"]["archetype_priority"]
    return _BRYAN_KIM_ARCHETYPE_PRIORITY


def test_bryan_kim_roundtrip_semantically_equivalent():
    high_level_icp = {
        "role": "Technical founder / CEO of a venture-scale AI startup",
        "seniority": "Founder / CEO / CTO",
        "co_stage": "Pre-seed to Series B",
        "format": "Intimate fireside chat + founder networking",  # -> exclusive band
        "city": "NYC",
        "goal": "Fill the room with ambitious technical AI founders.",
        "priority_archetypes": ["founder"],
        "deprioritize_archetypes": ["investor"],
        "require_corroboration": True,
    }
    cfg = compile_icp(high_level_icp)
    ap = cfg["archetype_priority"]
    ref = _load_reference_archetype_priority()

    # Semantic equivalence on the consumed keys.
    assert ap["boost"] == ref["boost"]
    assert ap["cap"] == ref["cap"]
    assert ap["require_corroboration_for_boost"] == ref["require_corroboration_for_boost"]
    assert ap["auto_accept"]["archetype"] == ref["auto_accept"]["archetype"]
    assert ap["auto_accept"]["require_corroboration"] == ref["auto_accept"]["require_corroboration"]
    assert ap["auto_accept"]["min_dimension"] == ref["auto_accept"]["min_dimension"]

    # Intimate format -> exclusive threshold band.
    assert cfg["thresholds"]["accept_fit_min"] == 78
    assert cfg["thresholds"]["maybe_fit_min"] == 60
    assert cfg["thresholds"]["reject_fit_max"] == 45

    # NYC + in-person -> location hard filter.
    assert "Must be based in NYC" in cfg["hard_filters"]
    assert cfg["event_goal"] == high_level_icp["goal"]


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_deterministic_same_input_same_output():
    icp = {
        "format": "dinner",
        "priority_archetypes": ["founder"],
        "deprioritize_archetypes": ["investor"],
        "city": "SF",
        "anti_fit": ["recruiters"],
    }
    assert compile_icp(dict(icp)) == compile_icp(dict(icp))
