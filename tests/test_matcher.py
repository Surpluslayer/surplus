"""Tests for agents/matcher.py — edge typing + side-balanced group formation."""
from types import SimpleNamespace

from backend.agents.matcher import build_edges, form_groups


def _p(pid, side, works_on="observability", score=85):
    return SimpleNamespace(id=pid, side=side, works_on=works_on,
                           fit_score=score, name=f"P{pid}")


def _event(fmt="Sit-down dinner"):
    return SimpleNamespace(format=fmt)


def test_cross_side_pairs_are_symbiotic():
    a = _p(1, "Builds")
    b = _p(2, "Hires")
    edges = build_edges([a, b])
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "symbiotic"


def test_same_side_adjacent_domain_is_affinity():
    a = _p(1, "Builds", works_on="model-serving")
    b = _p(2, "Builds", works_on="ml-platform")  # adjacent domains
    edges = build_edges([a, b])
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "affinity"


def test_same_side_unrelated_domain_has_no_edge():
    a = _p(1, "Builds", works_on="payments-infra")
    b = _p(2, "Builds", works_on="web-infra")  # not adjacent
    assert build_edges([a, b]) == []


def test_symbiotic_outweighs_affinity_at_equal_fit():
    sym = build_edges([_p(1, "Builds"), _p(2, "Hires")])[0]
    aff = build_edges([_p(1, "Builds", "model-serving"),
                       _p(2, "Builds", "ml-platform")])[0]
    assert sym["weight"] > aff["weight"]


def test_groups_partition_all_attendees():
    people = [_p(i, "Builds" if i % 2 else "Hires") for i in range(1, 11)]
    groups = form_groups(people, _event())
    placed = [m for members in groups.values() for m in members]
    assert sorted(p.id for p in placed) == [p.id for p in people]


def test_groups_balance_sides():
    # 6 builders + 3 counterparts -> no group should be builders-only
    people = ([_p(i, "Builds") for i in range(1, 7)]
              + [_p(i, "Hires") for i in range(7, 10)])
    groups = form_groups(people, _event())
    for members in groups.values():
        sides = {m.side for m in members}
        assert "Builds" in sides and len(sides) > 1
