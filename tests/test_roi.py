"""Tests for agents/roi.py — tier mapping, ledger pricing, net ROI math."""
from types import SimpleNamespace

from backend.agents.roi import tier_of, settle


def _p(pid, score, side="Builds"):
    return SimpleNamespace(id=pid, fit_score=score, side=side,
                           name=f"P{pid}", company="Acme")


def _event(goal="Hiring pipeline", budget=10000, headcount=12):
    return SimpleNamespace(goal=goal, budget=budget, headcount=headcount)


def test_tier_boundaries():
    assert tier_of(90) == "high"
    assert tier_of(89) == "mid"
    assert tier_of(82) == "mid"
    assert tier_of(81) == "low"


def test_ledger_has_one_row_per_guest():
    attending = [_p(1, 95), _p(2, 85), _p(3, 70)]
    ledger, _ = settle(_event(), attending)
    assert len(ledger) == 3
    assert {r["prospect_id"] for r in ledger} == {1, 2, 3}


def test_high_fit_converts_to_won():
    ledger, _ = settle(_event(), [_p(1, 95)])
    assert ledger[0]["state"] == "won"
    assert ledger[0]["value"] > 0


def test_low_fit_is_lost_and_zero_value():
    ledger, _ = settle(_event(), [_p(1, 60)])
    assert ledger[0]["state"] == "lost"
    assert ledger[0]["value"] == 0


def test_net_roi_math():
    # two 'won' hires at $28k each = $56k against a $10k budget -> 460%
    ledger, metrics = settle(_event(budget=10000), [_p(1, 95), _p(2, 92)])
    assert metrics["value_generated"] == 56000
    assert metrics["net_roi_pct"] == round((56000 - 10000) / 10000 * 100)
    assert metrics["converted"] == 2


def test_goal_changes_pricing():
    guests = [_p(1, 95)]
    hiring, _ = settle(_event(goal="Hiring pipeline"), guests)
    raising, _ = settle(_event(goal="Fundraising"), guests)
    # same guest, same fit — different goal prices the outcome differently
    assert hiring[0]["value"] != raising[0]["value"]
    assert hiring[0]["label"] != raising[0]["label"]


def test_ledger_sorted_by_value_desc():
    ledger, _ = settle(_event(), [_p(1, 60), _p(2, 95), _p(3, 85)])
    values = [r["value"] for r in ledger]
    assert values == sorted(values, reverse=True)
