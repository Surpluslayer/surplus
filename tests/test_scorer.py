"""Tests for agents/scorer.py — fit scoring + the floating threshold."""
from types import SimpleNamespace

from backend.agents.scorer import score_prospect, floating_threshold
from backend import config


def _prospect(**kw):
    base = dict(gh_stars=0, x_followers=0, seniority="Mid",
                li_resolved=False, sources="linkedin")
    base.update(kw)
    return SimpleNamespace(**base)


def _event(seniority="Senior"):
    return SimpleNamespace(seniority=seniority)


def test_score_is_bounded_0_100():
    strong = _prospect(gh_stars=9999, x_followers=9999,
                       seniority="Leadership", li_resolved=True,
                       sources="github,x,linkedin")
    weak = _prospect(gh_stars=0, x_followers=0, seniority="Mid",
                     li_resolved=False, sources="x")
    s_strong, _ = score_prospect(strong, _event())
    s_weak, _ = score_prospect(weak, _event())
    assert 0 <= s_weak <= s_strong <= 100


def test_more_signal_scores_higher():
    low = _prospect(gh_stars=10)
    high = _prospect(gh_stars=2000)
    assert score_prospect(high, _event())[0] > score_prospect(low, _event())[0]


def test_resolved_contact_helps():
    without = _prospect(li_resolved=False)
    withc = _prospect(li_resolved=True)
    assert score_prospect(withc, _event())[0] > score_prospect(without, _event())[0]


def test_reasoning_is_returned():
    _, reason = score_prospect(_prospect(gh_stars=2000, li_resolved=True), _event())
    assert isinstance(reason, str) and reason.endswith(".")


def test_threshold_floats_down_to_meet_supply():
    # the bar drops from 95 only as far as it must to clear the funnel target
    scores = [95, 92, 60, 58, 57]
    assert floating_threshold(scores, funnel_target=3) == 60


def test_threshold_floors_out_when_supply_too_thin():
    # too few candidates above the floor at any bar -> settles at the floor
    scores = [95, 92, 50, 48, 47]
    assert floating_threshold(scores, funnel_target=5) == config.ABS_FLOOR


def test_threshold_stays_high_for_rich_supply():
    scores = [95, 94, 93, 92, 91]
    t = floating_threshold(scores, funnel_target=3)
    assert t >= 90  # plenty of supply -> the bar holds high


def test_threshold_never_below_floor():
    scores = [10, 12, 14]
    assert floating_threshold(scores, funnel_target=3) == config.ABS_FLOOR
