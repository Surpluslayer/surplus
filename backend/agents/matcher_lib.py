"""
agents/matcher_lib.py — bridge from surplus's matcher to the vendored
`backend.matching` library (the real AI-driven matcher).

Surplus already has Prospects in the DB after /prospect runs. The library
expects `EnrichedPerson` dataclasses. This module:

  1. Maps surplus's `Prospect` ORM rows → library's `Person`
  2. Runs library `enrich_batch` (LLM + web_search per person; cached)
  3. Runs library `synthesize_rubric` for the event
  4. Runs library `compute_matrix` to score every pair
  5. Returns the matrix + a Prospect.id → top-K-pair-ids map ready for the
     surplus group-formation step to consume

Output stays compatible with `backend.agents.matcher.build_edges` shape so
the route handler doesn't need to change — same edge dicts, same group
formation, just better-weighted edges driven by the library's composite
score instead of `(avg_fit ± const)`.

Gated on `ANTHROPIC_API_KEY`. When the key is missing this module is
inert and `matcher.build_edges` falls back to the existing heuristic.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from ..matching.enrich import enrich_batch
from ..matching.matrix import compute_matrix
from ..matching.rubric import synthesize_rubric
from ..matching.schema import EnrichedPerson, Person


def library_available() -> bool:
    """True when ANTHROPIC_API_KEY is set — the library needs it for both
    enrichment and rubric synthesis. Returns False on any missing dep."""
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# ---- adapter: surplus Prospect → library Person ---------------------------

# Map surplus's market `side` field to the library's `ticket_type` enum.
# The rubric synthesizer reads ticket_type to decide pair-type weights, so
# the mapping should communicate role intent, not raw side labels.
_SIDE_TO_TICKET = {
    "Builds":   "Attendee",     # builders go in as general attendees
    "Hires":    "Hiring Lead",  # hirer side — looking to add to team
    "Operates": "Founder",      # operators are usually founders / GTM ops
}

_SENIORITY_TO_EXP = {
    "Mid":        "intermediate",
    "Senior":     "advanced",
    "Staff+":     "expert",
    "Leadership": "expert",
}


def prospect_to_person(p) -> Person:
    """Map a surplus Prospect ORM row to a library Person dataclass.

    Identifier fields not stored on Prospect (x_handle, github_username,
    email) are left blank — the library's enrichment step won't have those
    inputs to work with, but it can still scrape from linkedin_url and the
    GitHub API will skip when no username is provided.
    """
    return Person(
        id=f"prospect-{p.id}",
        name=p.name or f"Prospect {p.id}",
        role=p.role or "",
        title=p.role or "",
        company=p.company or "",
        linkedin_url=p.linkedin_url or "",
        ticket_type=_SIDE_TO_TICKET.get(p.side, "Attendee"),
        exp_level=_SENIORITY_TO_EXP.get(p.seniority, "unknown"),
    )


# ---- main entry -----------------------------------------------------------

def score_attendees(attending: list, event) -> Optional[dict[str, Any]]:
    """
    Run the library pipeline against `attending` (list of Prospect ORM rows)
    in the context of `event` (the surplus Event row).

    Returns the matrix dict from `compute_matrix`, or None if the library is
    unavailable / a step failed. The caller (matcher.build_edges) falls back
    to the existing heuristic on None.
    """
    if not library_available() or len(attending) < 2:
        return None
    try:
        people = [prospect_to_person(p) for p in attending]
        event_name = (
            f"{event.format} · {event.headcount}-person · "
            f"{event.city} · goal: {event.goal}"
        )
        event_desc = (
            f"A {event.format.lower()} in {event.city} for "
            f"{event.seniority} {event.role}. The hosting "
            f"organization is at the {event.co_stage} stage. The "
            f"goal is a {event.goal.lower()}. Budget: ${event.budget:,}."
        )

        # The library is fully async — drive it from a fresh event loop so
        # we can call it from the synchronous route handler.
        async def _run() -> dict[str, Any]:
            rubric = await synthesize_rubric(event_name, event_desc, people)
            enriched: list[EnrichedPerson] = await enrich_batch(people)
            return compute_matrix(enriched, rubric, top_k=min(8, len(people) - 1))

        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"  [matcher_lib] library scoring failed, falling back: "
              f"{type(exc).__name__}: {exc}")
        return None


# ---- edge builder using library scores -----------------------------------

def build_edges_from_matrix(matrix: dict[str, Any], attending: list) -> list[dict]:
    """
    Turn the library's pair scores into surplus's edge dicts.

    Library output for each pair:
      {
        a_id, b_id, composite (0..1), similar, complementary,
        role_pair, gate_passed, anti_multiplier, ...
      }

    Surplus edge dict:
      {a_id: int, b_id: int, edge_type: "symbiotic"|"affinity", weight: float}

    Heuristic for edge_type from the library output:
      - if role_pair_score is high (the pair is across complementary roles)
        AND complementary axis dominates → "symbiotic"
      - else (mostly similar axis) → "affinity"

    Weight is `composite * 100` so the scale matches the old heuristic's
    0-100ish range (form_groups doesn't read weight, but UI does).
    """
    edges: list[dict] = []
    # Map library person_id ("prospect-42") -> surplus prospect.id (42)
    id_lookup = {f"prospect-{p.id}": p.id for p in attending}

    for pair in matrix.get("pairs", []):
        if not pair.get("gate_passed", True):
            continue
        if pair.get("composite", 0) <= 0:
            continue
        a_id = id_lookup.get(pair["a_id"])
        b_id = id_lookup.get(pair["b_id"])
        if a_id is None or b_id is None:
            continue
        # Decide symbiotic vs affinity from which axis carried the score.
        similar = pair.get("similar", 0)
        complement = pair.get("complementary", 0)
        edge_type = "symbiotic" if complement > similar else "affinity"
        edges.append({
            "a_id": a_id,
            "b_id": b_id,
            "edge_type": edge_type,
            "weight": round(pair["composite"] * 100, 1),
        })
    return edges
