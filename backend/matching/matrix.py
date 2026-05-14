"""Build and query the full N×N pair-score matrix.

Given enriched people + rubric, produce:
  - pairs: list of all unique pair-score records (sparse — only ones that pass gates)
  - top_k_per_person: each person's top-K matches with full breakdown
  - mutual_pairs: pairs where each person is in the other's top-K (gold matches)

The matrix is the canonical output of the pipeline. The API/UI just renders it.

Storage: single JSON file at data/matches/<event_id>/matrix.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from backend.matching.schema import EnrichedPerson
from backend.matching.score import score_pair


MATRIX_DIR = Path("data/matches")
DEFAULT_TOP_K = 5


def compute_matrix(
    people: list[EnrichedPerson],
    rubric: dict[str, Any],
    *,
    top_k: int = DEFAULT_TOP_K,
    include_blocked: bool = False,
) -> dict[str, Any]:
    """Score every unique pair + derive top-K + mutual flags.

    Returns the full matrix dict (see schema below) — ready to JSON-serialize.

    Schema:
      {
        "event_id": "...",
        "people": [{id, name, ticket_type, company, title, city, ...}],  # denorm for UI
        "pairs": [
          {a_id, b_id, composite, similar_score, complementary_score,
           role_pair_score, anti_multiplier, components, gate_passed,
           anti_flags, mutual}
        ],
        "top_k_per_person": {
          person_id: [{other_id, composite, similar, complementary,
                       role, anti, components, mutual}, ...]
        },
        "mutual_pairs": [{a_id, b_id, composite}],
        "stats": {n_people, n_pairs_scored, n_pairs_passed, ...}
      }
    """
    t0 = time.time()
    n = len(people)
    if n < 2:
        return {
            "event_id": rubric.get("_event_id", ""),
            "people": [_denorm_person(p) for p in people],
            "pairs": [],
            "top_k_per_person": {},
            "mutual_pairs": [],
            "stats": {"n_people": n, "n_pairs_scored": 0, "n_pairs_passed": 0},
        }

    # --- 1) Score every unique pair ---
    pairs: list[dict[str, Any]] = []
    n_passed = 0
    n_blocked = 0
    for i in range(n):
        a = people[i]
        for j in range(i + 1, n):
            b = people[j]
            s = score_pair(a, b, rubric)
            passed = s["gate_passed"] and s["composite"] > 0
            if passed:
                n_passed += 1
            else:
                n_blocked += 1
            if passed or include_blocked:
                pairs.append({
                    "a_id": a.id,
                    "b_id": b.id,
                    "composite": s["composite"],
                    "similar_score": s["similar_score"],
                    "complementary_score": s["complementary_score"],
                    "role_pair_score": s["role_pair_score"],
                    "anti_multiplier": s["anti_multiplier"],
                    "gate_passed": s["gate_passed"],
                    "gate_failures": s.get("gate_failures", []),
                    "anti_flags": s.get("anti_flags", {}),
                    "components": s["components"],
                })

    # --- 2) Build top-K per person (only over passing pairs) ---
    per_person: dict[str, list[dict[str, Any]]] = {p.id: [] for p in people}
    for entry in pairs:
        if not entry["gate_passed"] or entry["composite"] <= 0:
            continue
        per_person[entry["a_id"]].append(_match_entry_for_view(entry, "b"))
        per_person[entry["b_id"]].append(_match_entry_for_view(entry, "a"))

    top_k_per_person: dict[str, list[dict[str, Any]]] = {}
    for pid, matches in per_person.items():
        matches.sort(key=lambda m: m["composite"], reverse=True)
        top_k_per_person[pid] = matches[:top_k]

    # --- 3) Mutual flags: each person in the other's top-K ---
    top_k_sets: dict[str, set[str]] = {
        pid: {m["other_id"] for m in matches}
        for pid, matches in top_k_per_person.items()
    }
    mutual_pairs: list[dict[str, Any]] = []
    for entry in pairs:
        a, b = entry["a_id"], entry["b_id"]
        is_mutual = (b in top_k_sets.get(a, set())) and (a in top_k_sets.get(b, set()))
        entry["mutual"] = is_mutual
        if is_mutual:
            mutual_pairs.append({
                "a_id": a,
                "b_id": b,
                "composite": entry["composite"],
            })
    # Also annotate top_k entries
    for pid, matches in top_k_per_person.items():
        for m in matches:
            m["mutual"] = m["other_id"] in top_k_sets.get(pid, set()) and \
                          pid in top_k_sets.get(m["other_id"], set())

    mutual_pairs.sort(key=lambda x: x["composite"], reverse=True)

    elapsed = round(time.time() - t0, 3)
    return {
        "event_id": rubric.get("_event_id", ""),
        "event_name": rubric.get("_event_name", ""),
        "rubric_summary": {
            "event_type": rubric.get("event_type"),
            "match_intent": rubric.get("match_intent"),
            "notes_for_humans": rubric.get("notes_for_humans"),
        },
        "people": [_denorm_person(p) for p in people],
        "pairs": pairs,
        "top_k_per_person": top_k_per_person,
        "mutual_pairs": mutual_pairs,
        "stats": {
            "n_people": n,
            "n_pairs_scored": n * (n - 1) // 2,
            "n_pairs_passed": n_passed,
            "n_pairs_blocked": n_blocked,
            "n_mutual_pairs": len(mutual_pairs),
            "elapsed_s": elapsed,
            "top_k": top_k,
        },
    }


def _denorm_person(p: EnrichedPerson) -> dict[str, Any]:
    """Compact denorm used in the UI/output. Carries the fields we render
    in match cards without re-loading the full enriched profile."""
    return {
        "id": p.id,
        "name": p.name,
        "company": p.company,
        "title": p.title,
        "role": p.role,
        "ticket_type": p.ticket_type,
        "city": p.city,
        "linkedin_url": p.linkedin_url,
        "x_handle": p.x_handle,
        "github_username": p.github_username,
        "exp_level": p.exp_level,
        "domains": p.domains,
        "tech_stack": p.tech_stack,
        "bio_text": p.bio_text,
        "enrichment_status": p.enrichment_status,
    }


def _match_entry_for_view(pair: dict[str, Any], from_perspective: str) -> dict[str, Any]:
    """Render a per-person top-K entry. `from_perspective` is 'a' or 'b' —
    which side of the pair is asking for matches."""
    other_id = pair["b_id"] if from_perspective == "b" else pair["a_id"]
    return {
        "other_id": other_id,
        "composite": pair["composite"],
        "similar_score": pair["similar_score"],
        "complementary_score": pair["complementary_score"],
        "role_pair_score": pair["role_pair_score"],
        "anti_multiplier": pair["anti_multiplier"],
        "components": pair["components"],
        "anti_flags": pair["anti_flags"],
    }


# ---- Persistence ----

def save_matrix(matrix: dict[str, Any], event_id: str | None = None) -> Path:
    """Write matrix.json + top_k.json to data/matches/<event_id>/."""
    eid = event_id or matrix.get("event_id") or "unknown"
    out_dir = MATRIX_DIR / eid
    out_dir.mkdir(parents=True, exist_ok=True)
    full_path = out_dir / "matrix.json"
    top_path = out_dir / "top_k.json"
    full_path.write_text(json.dumps(matrix, indent=2))
    # Slim file: just top_k + mutual_pairs + people (no full pair list)
    top_only = {
        "event_id": matrix.get("event_id"),
        "event_name": matrix.get("event_name"),
        "rubric_summary": matrix.get("rubric_summary"),
        "people": matrix.get("people"),
        "top_k_per_person": matrix.get("top_k_per_person"),
        "mutual_pairs": matrix.get("mutual_pairs"),
        "stats": matrix.get("stats"),
    }
    top_path.write_text(json.dumps(top_only, indent=2))
    return full_path


def load_matrix(event_id: str) -> dict[str, Any] | None:
    p = MATRIX_DIR / event_id / "matrix.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


# ---- Query helpers ----

def top_matches_for(matrix: dict[str, Any], person_id: str, k: int | None = None) -> list[dict[str, Any]]:
    matches = matrix.get("top_k_per_person", {}).get(person_id, [])
    return matches if k is None else matches[:k]


def mutual_pairs_top_n(matrix: dict[str, Any], n: int = 20) -> list[dict[str, Any]]:
    return matrix.get("mutual_pairs", [])[:n]


def person_by_id(matrix: dict[str, Any], person_id: str) -> dict[str, Any] | None:
    for p in matrix.get("people", []):
        if p["id"] == person_id:
            return p
    return None
