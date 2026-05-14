"""Deterministic per-pair scoring. No LLM. No I/O. Microseconds per pair.

Given two EnrichedPerson and the synthesized rubric, returns:
  - similar_score (0..1)            — shared context (gate)
  - complementary_score (0..1)      — value exchange (optimizer)
  - role_pair_score (0..1)          — looked up in rubric.role_pair_matrix
  - gate_passed (bool)              — hard gates from rubric
  - anti_multiplier (0..1)          — penalty for direct competitor, clone, etc.
  - composite (0..1)                — final match score
  - components (dict)               — breakdown of every sub-axis (for explain.py + UI)

Each sub-axis is a set-based operation on already-extracted tags. The rubric
decides weights; this module just applies them.

Math:
  composite = role_pair_score × anti_multiplier × sqrt(
                axis_blend.similar       × similar_score
              + axis_blend.complementary × complementary_score
            )
  (Geometric-style blend means a pair must score on BOTH axes, not just one.)
"""
from __future__ import annotations

import math
import re
from typing import Any

from backend.matching.schema import EnrichedPerson, EXP_LEVELS


# ---- Small helpers ----

_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "have",
    "ai", "ml", "ml-",  # too generic on their own
    "use", "using", "build", "building", "make", "making",
    "are", "was", "will", "should", "could", "would",
}


def _to_token_set(items: list[Any]) -> set[str]:
    """Convert a list of strings/sentences to a comparable token set.

    Used for fields like conviction_themes where each item is a free-text
    sentence ('humanoid form factor will win') rather than a tag.
    """
    tokens: set[str] = set()
    for it in items or []:
        if not isinstance(it, str):
            continue
        for tok in _TOKEN_RE.findall(it.lower()):
            if tok in _STOPWORDS:
                continue
            tokens.add(tok)
    return tokens


def _to_tag_set(items: list[Any]) -> set[str]:
    """Convert a list of canonical tags (already-normalized) to a set."""
    out: set[str] = set()
    for it in items or []:
        if isinstance(it, str) and it.strip():
            out.add(it.strip().lower())
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _jaccard_distance(a: set[str], b: set[str]) -> float:
    """1.0 = totally complementary (no overlap). 0.0 = identical."""
    if not a and not b:
        return 0.0  # both empty — no signal, treat as no complementarity
    if not a or not b:
        return 0.5  # one has stack, one doesn't — partial complement
    return 1.0 - _jaccard(a, b)


def _exp_level_distance(a: str, b: str) -> int:
    """Distance in EXP_LEVELS buckets. Unknown contributes 0 (no info)."""
    levels = [l for l in EXP_LEVELS if l != "unknown"]
    if a == "unknown" or b == "unknown":
        return 0
    try:
        return abs(levels.index(a) - levels.index(b))
    except ValueError:
        return 0


def _city_match(a: str, b: str) -> float:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Soft match: shared city token (e.g. "san francisco" vs "san francisco, ca")
    a_set = set(a.split())
    b_set = set(b.split())
    if "san" in a_set and "san" in b_set and ("francisco" in a_set and "francisco" in b_set):
        return 1.0
    return 0.6 if (a_set & b_set) else 0.0


def _past_companies(p: EnrichedPerson) -> set[str]:
    out: set[str] = set()
    if p.company:
        out.add(p.company.strip().lower())
    for r in p.roles_history or []:
        c = (r or {}).get("company", "")
        if c:
            out.add(str(c).strip().lower())
    # GitHub field too
    for r in p.github_top_repos or []:
        # rarely useful for company; skip
        pass
    return {c for c in out if c and c not in {"n/a", "na", "stealth", "unknown", ""}}


def _schools(p: EnrichedPerson) -> set[str]:
    out: set[str] = set()
    for r in p.roles_history or []:
        # Some enrichments mix schools into roles_history; check title for student keywords
        title = (r.get("title") or "").lower()
        comp = (r.get("company") or "").lower()
        if "student" in title or "university" in comp or "college" in comp or "school" in comp:
            out.add(comp)
    return out


# ---- Sub-axis scorers (similar axis) ----

def _score_domain_overlap(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """Exact-tag Jaccard + half-credit for kebab-case prefix matches.

    Why: domains are extracted at fine granularity (robotics-manipulation,
    humanoid-robotics, ml-infra). Two people in the same broad field rarely
    have identical tags. Prefix matching ('robotics-*' ↔ 'robotics-*')
    gives soft credit for shared broad area.
    """
    a_set = _to_tag_set(a.domains)
    b_set = _to_tag_set(b.domains)
    if not a_set or not b_set:
        return 0.0
    exact = _jaccard(a_set, b_set)

    def _prefix(t: str) -> str:
        return t.split("-")[0]

    a_pref = {_prefix(t) for t in a_set}
    b_pref = {_prefix(t) for t in b_set}
    prefix_match = _jaccard(a_pref, b_pref)
    # Exact matches count fully; prefix-only matches count half
    return min(1.0, exact + 0.5 * max(0.0, prefix_match - exact))


def _score_conviction_overlap(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """Conviction themes are free-text sentences — compare via token sets."""
    return _jaccard(_to_token_set(a.conviction_themes), _to_token_set(b.conviction_themes))


def _score_background_resonance(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """Shared past employers + shared schools = trust accelerant."""
    companies = _jaccard(_past_companies(a), _past_companies(b))
    schools = _jaccard(_schools(a), _schools(b))
    # Weight companies higher than schools — same company is rarer + stronger
    return min(1.0, 0.6 * companies + 0.4 * schools)


def _score_city_match(a: EnrichedPerson, b: EnrichedPerson) -> float:
    return _city_match(a.city, b.city)


# ---- Sub-axis scorers (complementary axis) ----

def _score_skill_complement(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """High = different tech stacks (different things to bring to the table)."""
    return _jaccard_distance(_to_tag_set(a.tech_stack), _to_tag_set(b.tech_stack))


def _score_experience_asymmetry(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """Higher exp-level gap → potential mentor relationship.

    Caps at 3 buckets gap (max useful asymmetry — beyond that becomes anti-signal).
    """
    dist = _exp_level_distance(a.exp_level, b.exp_level)
    return min(dist / 3.0, 1.0)


def _score_role_complement(a: EnrichedPerson, b: EnrichedPerson, rubric: dict[str, Any]) -> float:
    """Use role_pair_matrix as a soft complementarity signal too.

    If the rubric says Attendee|Investor = 0.5, that's a moderate complement.
    Identical roles cap at 0.5 (some complement potential within same role).
    """
    matrix = rubric.get("role_pair_matrix", {})
    a_t = a.ticket_type or "unknown"
    b_t = b.ticket_type or "unknown"
    key = f"{a_t}|{b_t}"
    val = matrix.get(key)
    if val is None:
        val = matrix.get(f"{b_t}|{a_t}", 0.5)
    return float(val)


def _score_domain_expansion(a: EnrichedPerson, b: EnrichedPerson) -> float:
    """Same broad area, different specific angles.

    Soft signal: each side has at least one domain the other lacks.
    Peaks when domain sets share a 'theme prefix' (kebab-case slug) but
    differ in specifics. E.g. 'robotics-manipulation' vs 'robotics-locomotion'.
    """
    a_set = _to_tag_set(a.domains)
    b_set = _to_tag_set(b.domains)
    if not a_set or not b_set:
        return 0.0

    def _prefix(t: str) -> str:
        return t.split("-")[0]

    a_prefixes = {_prefix(t) for t in a_set}
    b_prefixes = {_prefix(t) for t in b_set}
    shared_prefixes = a_prefixes & b_prefixes
    if not shared_prefixes:
        return 0.0
    # Each side has a unique specific domain in a shared prefix area
    a_unique = a_set - b_set
    b_unique = b_set - a_set
    a_has_unique_in_shared = any(_prefix(t) in shared_prefixes for t in a_unique)
    b_has_unique_in_shared = any(_prefix(t) in shared_prefixes for t in b_unique)
    if a_has_unique_in_shared and b_has_unique_in_shared:
        return 1.0
    if a_has_unique_in_shared or b_has_unique_in_shared:
        return 0.5
    return 0.0


# ---- Anti-signals ----

def _direct_competitor(a: EnrichedPerson, b: EnrichedPerson) -> bool:
    """Same domain + same primary role keyword = building the same thing."""
    a_d = _to_tag_set(a.domains)
    b_d = _to_tag_set(b.domains)
    if not (a_d and b_d and a_d & b_d):
        return False
    # If both founder/CEO/CTO at companies in the same domain, suspicious
    founder_tokens = {"founder", "ceo", "cto", "co-founder", "founding"}
    a_role_tokens = _TOKEN_RE.findall((a.role + " " + a.title).lower())
    b_role_tokens = _TOKEN_RE.findall((b.role + " " + b.title).lower())
    a_is_founder = any(t in founder_tokens for t in a_role_tokens)
    b_is_founder = any(t in founder_tokens for t in b_role_tokens)
    if not (a_is_founder and b_is_founder):
        return False
    # And in same domain
    return bool(a_d & b_d)


def _profile_clone(a: EnrichedPerson, b: EnrichedPerson) -> bool:
    """Very high similarity on EVERYTHING → no exchange potential."""
    domains = _jaccard(_to_tag_set(a.domains), _to_tag_set(b.domains))
    stack = _jaccard(_to_tag_set(a.tech_stack), _to_tag_set(b.tech_stack))
    return domains >= 0.7 and stack >= 0.7


def _seniority_gap_3plus(a: EnrichedPerson, b: EnrichedPerson) -> bool:
    return _exp_level_distance(a.exp_level, b.exp_level) >= 3


# ---- Composite ----

def score_pair(
    a: EnrichedPerson,
    b: EnrichedPerson,
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Score one pair. Pure math, no I/O. Returns full breakdown."""
    weights = rubric.get("weights", {})
    similar_w = weights.get("similar", {})
    complement_w = weights.get("complementary", {})
    axis_blend = weights.get("axis_blend", {"similar": 0.3, "complementary": 0.7})
    anti = rubric.get("anti_signals", {})
    gates = rubric.get("hard_gates", {})

    # --- Similar axis sub-components ---
    sim_components = {
        "domain_overlap": _score_domain_overlap(a, b),
        "conviction_overlap": _score_conviction_overlap(a, b),
        "background_resonance": _score_background_resonance(a, b),
        "city_match": _score_city_match(a, b),
    }
    similar_score = sum(
        sim_components[k] * similar_w.get(k, 0.0)
        for k in sim_components
    )

    # --- Complementary axis sub-components ---
    comp_components = {
        "skill_complement": _score_skill_complement(a, b),
        "experience_asymmetry": _score_experience_asymmetry(a, b),
        "role_complement": _score_role_complement(a, b, rubric),
        "domain_expansion": _score_domain_expansion(a, b),
    }
    complementary_score = sum(
        comp_components[k] * complement_w.get(k, 0.0)
        for k in comp_components
    )

    # --- Role pair score (used as multiplier on composite) ---
    role_pair_score = _score_role_complement(a, b, rubric)

    # --- Anti-signal multipliers ---
    anti_mult = 1.0
    anti_flags: dict[str, bool] = {}
    if _direct_competitor(a, b):
        anti_mult *= anti.get("direct_competitor_multiplier", 0.25)
        anti_flags["direct_competitor"] = True
    if _profile_clone(a, b):
        anti_mult *= anti.get("profile_clone_multiplier", 0.70)
        anti_flags["profile_clone"] = True
    if _seniority_gap_3plus(a, b):
        anti_mult *= anti.get("seniority_gap_3_or_more_multiplier", 0.65)
        anti_flags["seniority_gap_3_or_more"] = True

    # --- Hard gates ---
    gate_passed = True
    gate_failures: list[str] = []
    if similar_score < gates.get("min_similar_score", 0.0):
        gate_passed = False
        gate_failures.append("min_similar_score")
    if role_pair_score < gates.get("min_role_pair_score", 0.0):
        gate_passed = False
        gate_failures.append("min_role_pair_score")
    if gates.get("require_same_city") and sim_components["city_match"] < 1.0:
        gate_passed = False
        gate_failures.append("require_same_city")

    # --- Composite ---
    # Weighted sum across the two axes — honors the rubric's axis_blend
    # without zeroing out lopsided pairs (e.g. great complementarity but
    # no shared specifics). The role_pair_score multiplier handles
    # role-incompatibility filtering instead.
    blend_sim = axis_blend.get("similar", 0.3)
    blend_comp = axis_blend.get("complementary", 0.7)
    blended = blend_sim * similar_score + blend_comp * complementary_score
    blended = min(blended, 1.0)
    composite = role_pair_score * anti_mult * blended if gate_passed else 0.0

    return {
        "composite": round(composite, 4),
        "similar_score": round(similar_score, 4),
        "complementary_score": round(complementary_score, 4),
        "role_pair_score": round(role_pair_score, 4),
        "anti_multiplier": round(anti_mult, 4),
        "gate_passed": gate_passed,
        "gate_failures": gate_failures,
        "anti_flags": anti_flags,
        "components": {
            "similar": {k: round(v, 4) for k, v in sim_components.items()},
            "complementary": {k: round(v, 4) for k, v in comp_components.items()},
        },
    }
