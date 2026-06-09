"""Tests for the offline voice/grounding eval harness (scripts/voice_eval.py).

These pin two things the harness must get right to be trustworthy, both
WITHOUT touching the network:

  1. build_prompts() ablates the two new layers correctly — variant A sends
     neither <context_brief> nor <host_voice_profile>, B sends only the brief,
     C only the profile, D both. If the toggles leak, every comparison the
     harness reports is meaningless, so we guard them tightly.

  2. score_draft() actually fires on the failure modes it claims to measure —
     a leaked em-dash trips dash_clean, an un-sourced fact trips grounded, the
     real open-loop hook trips references_hook, length/greeting/emoji track the
     host profile. A scorer that can't fail proves nothing.

Kept API-free on purpose (deterministic mode only); --live is exercised by hand.
"""
from __future__ import annotations

from scripts import voice_eval as ve


# ── fixtures ──────────────────────────────────────────────────────────────────

def _case_by_name(name: str) -> ve.EvalCase:
    for c in ve.cases():
        if c.name == name:
            return c
    raise AssertionError(f"no eval case named {name!r}")


def _variant(key: str) -> ve.Variant:
    return next(v for v in ve.VARIANTS if v.key == key)


CASE = "owed_resource_casual_host"


# ── build_prompts: the ablation toggles ──────────────────────────────────────

def test_variant_A_sends_neither_layer():
    case = _case_by_name(CASE)
    p = ve.build_prompts(case, _variant("A"))
    assert "<host_voice_profile>" not in p["system"]
    assert "<context_brief>" not in p["user"]
    # baseline still carries the style examples (pre-Step-1/2 behavior)
    assert "<style_examples>" in p["system"]


def test_variant_B_adds_only_the_brief():
    case = _case_by_name(CASE)
    p = ve.build_prompts(case, _variant("B"))
    assert "<context_brief>" in p["user"]
    assert "<host_voice_profile>" not in p["system"]


def test_variant_C_adds_only_the_profile():
    case = _case_by_name(CASE)
    p = ve.build_prompts(case, _variant("C"))
    assert "<host_voice_profile>" in p["system"]
    assert "<context_brief>" not in p["user"]


def test_variant_D_adds_both_layers():
    case = _case_by_name(CASE)
    p = ve.build_prompts(case, _variant("D"))
    assert "<host_voice_profile>" in p["system"]
    assert "<context_brief>" in p["user"]
    assert "<style_examples>" in p["system"]


def test_brief_block_is_real_context_brief_json():
    """The brief the harness embeds must be the prod _context_brief output, not
    a hand-rolled stand-in — otherwise variant B doesn't test what ships."""
    import json
    case = _case_by_name(CASE)
    p = ve.build_prompts(case, _variant("B"))
    expected = ve.ragent._context_brief(case.sel, case.ctx)
    assert json.dumps(expected, default=str) in p["user"]


def test_all_variants_share_the_same_triage_signal():
    case = _case_by_name(CASE)
    for key in ("A", "B", "C", "D"):
        u = ve.build_prompts(case, _variant(key))["user"]
        assert "<triage_signal>" in u
        assert case.sel["reason"] in u


# ── score_draft: failure-mode detection ──────────────────────────────────────

def _profile(case: ve.EvalCase) -> dict:
    return ve.voice.build_host_voice_profile(case.host_examples)


def test_dash_clean_trips_on_leaked_em_dash():
    case = _case_by_name(CASE)
    clean = ve.score_draft("Hey Sarah! here's the deck 🙌", case, _profile(case))
    dashed = ve.score_draft("Hey Sarah! here's the deck — as promised 🙌",
                            case, _profile(case))
    assert clean["dash_clean"] is True
    assert dashed["dash_clean"] is False


def test_grounded_trips_on_unsourced_fact():
    case = _case_by_name(CASE)  # forbidden incl. "series a", "funding"
    ok = ve.score_draft("Hey Sarah! here's the deck 🙌", case, _profile(case))
    bad = ve.score_draft("Hey Sarah! congrats on the Series A, here's the deck 🙌",
                         case, _profile(case))
    assert ok["grounded"] is True
    assert bad["grounded"] is False


def test_references_hook_detects_the_open_loop():
    case = _case_by_name(CASE)  # must_reference == ["deck"]
    hit = ve.score_draft("Hey! here's the deck", case, _profile(case))
    miss = ve.score_draft("Hey! great catching up", case, _profile(case))
    assert hit["references_hook"] is True
    assert miss["references_hook"] is False


def test_references_hook_is_none_when_case_has_no_hook():
    case = _case_by_name("their_court_should_be_brief_aware")  # must_reference == []
    sc = ve.score_draft("Just bumping this up", case, _profile(case))
    assert sc["references_hook"] is None


def test_length_ok_trips_when_draft_exceeds_band():
    case = _case_by_name(CASE)  # casual host → "short" band (<=35 words)
    long_draft = " ".join(["word"] * 60)
    sc = ve.score_draft(long_draft, case, _profile(case))
    assert sc["length_ok"] is False


def test_greeting_and_emoji_match_track_the_profile():
    case = _case_by_name(CASE)  # greeting "hey", uses_emoji True
    on = ve.score_draft("Hey Sarah! here's the deck 🙌", case, _profile(case))
    assert on["greeting_match"] is True
    assert on["emoji_match"] is True
    off = ve.score_draft("Hi Sarah, here is the deck.", case, _profile(case))
    assert off["greeting_match"] is False   # wrong greeting
    assert off["emoji_match"] is False      # missing the host's emoji habit


def test_skip_outcome_is_not_scored_for_voice():
    """A held message (skip) must NOT be penalized on dash/length/greeting — its
    reason text is internal reasoning, never sent. This is the exact bug the
    first live run exposed: D correctly skipped but scored worse than baseline."""
    case = _case_by_name("their_court_should_be_brief_aware")  # expect == "skip"
    skip = ("[skip] Tom said he'd circle back — the ball is in his court and "
            "it's too soon to nudge again, so holding off.")
    sc = ve.score_draft(skip, case, _profile(case))
    assert sc["outcome"] == "skip"
    assert sc["outcome_ok"] is True           # holding was the right call
    assert sc["dash_clean"] is None           # em-dash in the REASON isn't graded
    assert sc["length_ok"] is None
    assert sc["greeting_match"] is None
    assert sc["emoji_match"] is None
    assert sc["grounded"] is True             # grounding still applies
    assert sc["score"] == 1.0                 # right action + grounded == perfect


def test_drafting_when_case_wants_a_hold_fails_outcome():
    """The canned over-eager nudge on a 'should skip' case is the planted failure
    mode: it drafts when it should hold, so outcome_ok must flag it."""
    case = _case_by_name("their_court_should_be_brief_aware")
    sc = ve.score_draft(case.canned_draft, case, _profile(case))
    assert sc["outcome"] == "draft"
    assert sc["outcome_ok"] is False


def test_skipping_when_case_wants_a_draft_fails_outcome():
    case = _case_by_name(CASE)  # owed_resource, expect == "draft"
    sc = ve.score_draft("[skip] nothing to do here", case, _profile(case))
    assert sc["outcome"] == "skip"
    assert sc["outcome_ok"] is False


def test_either_case_does_not_grade_outcome():
    case = _case_by_name("stale_reconnect_no_history_to_invent")  # expect "either"
    drafted = ve.score_draft("Hey Mia! been a while, how are things?", case,
                             _profile(case))
    held = ve.score_draft("[skip] ball is in her court, no hook", case,
                          _profile(case))
    assert drafted["outcome_ok"] is None
    assert held["outcome_ok"] is None


def test_grounding_is_graded_even_on_a_skip():
    """A skip reason that invents a forbidden fact is still a hallucination tell."""
    case = _case_by_name("stale_reconnect_no_history_to_invent")
    sc = ve.score_draft("[skip] she just raised a Series A so I'll wait", case,
                        _profile(case))
    assert sc["outcome"] == "skip"
    assert sc["grounded"] is False


def test_voice_metrics_are_none_without_a_profile():
    """A/B variants score with profile=None — voice metrics must abstain (None),
    not silently pass/fail, so the score reflects only the graded dimensions."""
    case = _case_by_name(CASE)
    sc = ve.score_draft("Hey! here's the deck 🙌", case, None)
    assert sc["greeting_match"] is None
    assert sc["emoji_match"] is None
    assert sc["length_ok"] is True  # length_ok still grades against the default band


def test_score_is_mean_of_graded_metrics_only():
    case = _case_by_name(CASE)
    sc = ve.score_draft("Hey Sarah! here's the deck 🙌", case, _profile(case))
    graded = [v for k, v in sc.items()
              if k != "score" and isinstance(v, bool)]
    assert sc["score"] == round(sum(graded) / len(graded), 3)
    assert 0.0 <= sc["score"] <= 1.0


# ── the canned-draft fixtures stay internally consistent ─────────────────────

def test_canned_drafts_pass_their_own_grounding():
    """Every case's canned_draft must be clean + grounded + on-hook; they are
    the harness's 'known-good' anchor, so a typo that makes one fail grounding
    would quietly poison the deterministic scorecard."""
    for case in ve.cases():
        prof = ve.voice.build_host_voice_profile(case.host_examples)
        sc = ve.score_draft(case.canned_draft, case, prof)
        assert sc["dash_clean"] is True, case.name
        assert sc["grounded"] is True, case.name
        if case.must_reference:
            assert sc["references_hook"] is True, case.name
