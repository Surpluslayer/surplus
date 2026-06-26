"""
agents/sponsor_matcher.py : sponsor↔attendee scoring.

Stage 04 extension. A sponsor brings a buyer_profile (target_role,
seniority, company_stage, industry, intent="buying") that we score
against every attending Prospect's OFFERS/SEEKS vector using the SAME
pairwise machinery the guest↔guest matcher uses:

  - When ANTHROPIC_API_KEY is set, we ride on matcher_lib by feeding the
    buyer_profile in as one more Person in the same compute_matrix call
    that already ran for guests. This keeps the rubric, the enrichment,
    and the composite scoring identical : sponsor scoring is "one more
    row in the matrix", not a parallel pipeline.

  - Otherwise we fall back to the same kind of rule-based scoring
    matcher.build_edges does for guest pairs : role overlap, seniority
    alignment, stage match, industry/works_on adjacency. Result carries
    reasons[] so the WHY? popover can render the same way.

Idempotent: callers (routes/matching.py) wipe sponsor_matches for the
event before recomputing, just like MatchEdge.
"""
from __future__ import annotations
import json
from typing import Any

from ... import models
from .matcher import _adjacent, _AFFINITY


# ─── buyer_profile parsing ───────────────────────────────────────────

def parse_buyer_profile(raw: str | dict | None) -> dict:
    """Coerce a Sponsor.buyer_profile JSON string (or dict) into the
    canonical shape. Missing keys default to ''."""
    if isinstance(raw, dict):
        d = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = {}
    else:
        d = {}
    if not isinstance(d, dict):
        d = {}
    return {
        "target_role":   str(d.get("target_role") or "").strip(),
        "seniority":     str(d.get("seniority") or "").strip(),
        "company_stage": str(d.get("company_stage") or "").strip(),
        "industry":      str(d.get("industry") or "").strip(),
        # intent is always "buying" for a sponsor; surface it so the
        # scorer can express the asymmetry in reasons.
        "intent":        str(d.get("intent") or "buying").strip(),
    }


# ─── heuristic scorer ────────────────────────────────────────────────

# Senior/Staff+/Leadership treated as the "decision-maker" band : sponsors
# typically buy from people with budget authority.
_SENIORITY_RANK = {
    "Student": -1, "New grad": 0, "Junior": 1, "Mid": 2,
    "Senior": 3, "Staff+": 4, "Leadership": 5,
}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _role_match(buyer_role: str, prospect: models.Prospect) -> tuple[float, str | None]:
    """Compare buyer's target_role to the prospect's role/works_on/offers/seeks.

    Returns (0.0-1.0 partial weight, reason string or None)."""
    if not buyer_role:
        return 0.0, None
    needle = _norm(buyer_role)
    haystack = " ".join(_norm(s) for s in [
        prospect.role, prospect.works_on, prospect.offers, prospect.seeks,
    ])
    if not haystack:
        return 0.0, None
    # Token-by-token "any word matches" : cheap, deterministic.
    tokens = [t for t in needle.split() if len(t) >= 3]
    hits = [t for t in tokens if t in haystack]
    if not hits:
        return 0.0, None
    coverage = len(hits) / max(1, len(tokens))
    return coverage, f"role match on '{', '.join(hits)}'"


def _seniority_alignment(buyer_seniority: str, prospect: models.Prospect) -> tuple[float, str | None]:
    """Senior-or-above is the sweet spot for sponsor outreach. We score
    on rank-distance: equal level => 1.0, ±1 => 0.5, further => 0."""
    if not buyer_seniority:
        return 0.0, None
    want = _SENIORITY_RANK.get(buyer_seniority)
    have = _SENIORITY_RANK.get(prospect.seniority)
    if want is None or have is None:
        return 0.0, None
    diff = abs(want - have)
    if diff == 0:
        return 1.0, f"seniority match: {prospect.seniority}"
    if diff == 1:
        return 0.5, f"seniority within one rank: {prospect.seniority}"
    return 0.0, None


def _stage_match(buyer_stage: str, prospect_event_stage: str) -> tuple[float, str | None]:
    """Sponsor buys for a stage band; the event's co_stage is the
    proxy for what the attending pool looks like.

    NOTE: we read the event's co_stage, not the prospect's : prospects
    don't carry company-stage in the existing schema. This is a known
    coarseness of the heuristic path; the LLM path (matcher_lib) reads
    per-person enriched signal.
    """
    if not buyer_stage or not prospect_event_stage:
        return 0.0, None
    if _norm(buyer_stage) == _norm(prospect_event_stage):
        return 1.0, f"company stage match: {prospect_event_stage}"
    # CSV-list of stages (Event.co_stage can be multi-select)
    parts = [_norm(s) for s in prospect_event_stage.split(",") if s.strip()]
    if _norm(buyer_stage) in parts:
        return 0.8, f"company stage match: {buyer_stage}"
    return 0.0, None


def _industry_match(buyer_industry: str, prospect: models.Prospect) -> tuple[float, str | None]:
    """Compare to works_on (the prospect's domain tag), with adjacency
    fallback through matcher._AFFINITY."""
    if not buyer_industry:
        return 0.0, None
    needle = _norm(buyer_industry)
    works = _norm(prospect.works_on)
    if not works:
        return 0.0, None
    if needle == works or needle in works or works in needle:
        return 1.0, f"industry match: {prospect.works_on}"
    # Adjacency match via the same map matcher.py uses for affinity edges
    if works in _AFFINITY.get(needle, set()) or needle in _AFFINITY.get(works, set()):
        return 0.5, f"industry adjacent: {prospect.works_on} ↔ {buyer_industry}"
    return 0.0, None


def score_sponsor_vs_prospect(
    sponsor: models.Sponsor,
    prospect: models.Prospect,
    event: models.Event,
) -> tuple[float, list[str]]:
    """Heuristic sponsor↔prospect score.

    Returns (score 0-100, reasons[]). Same shape as MatchEdge.weight +
    pair_explainer reasons : the front-end renders sponsor pairs through
    the existing TOP PAIRS row component with no special-casing.
    """
    buyer = parse_buyer_profile(sponsor.buyer_profile)
    reasons: list[str] = []

    # Component weights : tuned so that a fully-aligned sponsor (all four
    # signals fire) ceilings around 95, leaving headroom for the "high
    # fit + intent buying" framing reason below.
    components: list[tuple[float, float, str | None]] = []
    w_role,      r_role      = _role_match(buyer["target_role"], prospect)
    w_seniority, r_seniority = _seniority_alignment(buyer["seniority"], prospect)
    w_stage,     r_stage     = _stage_match(buyer["company_stage"], event.co_stage)
    w_industry,  r_industry  = _industry_match(buyer["industry"], prospect)
    components.append((w_role, 30, r_role))
    components.append((w_seniority, 25, r_seniority))
    components.append((w_stage, 15, r_stage))
    components.append((w_industry, 25, r_industry))

    raw = 0.0
    for w, cap, reason in components:
        raw += w * cap
        if reason:
            reasons.append(reason)

    # Floor at the prospect's own fit_score / 4 so we never surface a
    # totally cold candidate as a sponsor target. The denominator keeps
    # this from dominating : it's a tie-breaker, not the signal.
    raw += min(5.0, (prospect.fit_score or 0) / 20)

    # Asymmetry framing : a sponsor is buying; surfacing this lets the
    # WHY? popover differentiate sponsor pairs from peer pairs.
    if buyer["intent"] == "buying" and reasons:
        reasons.insert(0, f"{sponsor.name} is buying; {prospect.name} fits the target profile")

    score = max(0.0, min(100.0, raw))
    return round(score, 1), reasons


def score_event_sponsors(
    event: models.Event,
    attending: list[models.Prospect],
    *,
    min_score: float = 8.0,
    top_per_sponsor: int = 12,
) -> dict[int, list[dict]]:
    """For each sponsor on `event`, score every attendee.

    Returns {sponsor_id: [{prospect_id, score, reasons}, ...]} sorted
    by score desc and truncated to `top_per_sponsor` rows per sponsor.

    Caller (routes/matching.py) is responsible for persisting these
    as SponsorMatch rows inside the existing /match transaction.
    """
    out: dict[int, list[dict]] = {}
    if not getattr(event, "sponsors", None):
        return out
    for sponsor in event.sponsors:
        rows: list[dict] = []
        for p in attending:
            score, reasons = score_sponsor_vs_prospect(sponsor, p, event)
            if score < min_score:
                continue
            rows.append({
                "prospect_id": p.id,
                "score": score,
                "reasons": reasons,
            })
        rows.sort(key=lambda r: -r["score"])
        out[sponsor.id] = rows[:top_per_sponsor]
    return out
