"""
agents/matcher.py — stage 04, the symbiotic matching market.

Edges are not friendship — they are *predicted total value created* by putting
two guests together. Two kinds:

  symbiotic : the two sit on different market sides, so one's offer can meet
              the other's seek (a builder and someone who can hire them; a
              founder and an investor). This is the value the matcher exists
              to manufacture.
  affinity  : same side, adjacent domains — they worked on similar things, so
              collaboration is easy. Useful, but a tiebreak, not the objective.

build_edges() scores every pair. form_groups() then packs guests into the
format's groups (Table / Team / ...), balancing sides so every group has both
offers and seeks in the room.

NOTE — open design question: build_edges weights symbiotic as a flat
(avg_fit + 10). The real objective function should weight *which* cross-side
pairing it is (founder<->investor vs builder<->hirer are not worth the same)
and feed that into form_groups. That weighting is the actual product decision;
this is a defensible placeholder.
"""
from __future__ import annotations
from itertools import combinations

from .. import config

# domains that count as "adjacent" for affinity edges
_AFFINITY = {
    "model-serving": {"ml-platform", "distributed-systems"},
    "ml-platform": {"model-serving", "data-infra"},
    "distributed-systems": {"model-serving", "observability"},
    "observability": {"distributed-systems", "data-infra"},
    "data-infra": {"observability", "ml-platform", "payments-infra"},
    "payments-infra": {"data-infra"},
    "web-infra": {"observability"},
}


def _adjacent(a: str, b: str) -> bool:
    return a == b or b in _AFFINITY.get(a, set()) or a in _AFFINITY.get(b, set())


def build_edges(attending: list) -> list[dict]:
    """Score every pair of confirmed guests. Returns edge dicts ready to persist."""
    edges: list[dict] = []
    for a, b in combinations(attending, 2):
        avg = (a.fit_score + b.fit_score) / 2
        if a.side != b.side:
            edges.append({"a_id": a.id, "b_id": b.id,
                          "edge_type": "symbiotic", "weight": round(avg + 10, 1)})
        elif _adjacent(a.works_on, b.works_on):
            edges.append({"a_id": a.id, "b_id": b.id,
                          "edge_type": "affinity", "weight": round(avg - 8, 1)})
    return edges


def form_groups(attending: list, event) -> dict[int, list]:
    """
    Pack confirmed guests into the format's groups, balancing market sides.

    Strategy: round-robin the non-builder sides (Hires / Operates) across the
    groups first — they're the scarce counterpart — then round-robin the
    builders. Every group ends up with offers and seeks in the room.
    """
    size = config.format_cfg(event.format)["group_size"]
    n_groups = max(1, round(len(attending) / size))
    groups: dict[int, list] = {i: [] for i in range(1, n_groups + 1)}

    builders = sorted((p for p in attending if p.side == "Builds"),
                      key=lambda p: -p.fit_score)
    counterparts = sorted((p for p in attending if p.side != "Builds"),
                          key=lambda p: -p.fit_score)

    for i, p in enumerate(counterparts):
        groups[(i % n_groups) + 1].append(p)
    for i, p in enumerate(builders):
        groups[(i % n_groups) + 1].append(p)

    return groups
