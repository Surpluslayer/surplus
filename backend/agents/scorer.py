"""
agents/scorer.py : stage 03a, fit scoring.

Two pieces:
  score_prospect(p, event) -> (score, reason)
      Deterministic 0-100 fit score with a human-readable rationale. Built from
      source signal strength + ICP match. Deterministic on purpose: the same
      inputs must always produce the same score so the threshold is stable and
      the result is auditable.

  floating_threshold(scores, funnel_target) -> int
      Fit is not a binary. The accept line *floats*: it drops only as far as it
      must to clear the funnel target, and never below ABS_FLOOR. Tight pool ->
      high bar; thin pool -> the bar gives, but only to the floor.

Accepts the Prospect ORM object directly (or any object with the same
attributes : tests pass a SimpleNamespace).
"""
from __future__ import annotations

from .. import config

_SENIORITY_RANK = {
    # Student sits just below New grad : someone still in school, often
    # interning. Using -1 (instead of shifting everything up by one) keeps
    # the existing fit-score thresholds stable for old events / cached pools.
    "Student": -1,
    "New grad": 0,
    "Junior": 1,
    "Mid": 2,
    "Senior": 3,
    "Staff+": 4,
    "Leadership": 5,
}


def score_prospect(p, event) -> tuple[int, str]:
    """Return (fit_score, reasoning) for a prospect against an event's ICP."""
    score = 40
    reasons: list[str] = []

    # --- source signal strength -------------------------------------------
    if p.gh_stars >= 1500:
        score += 18
        reasons.append("strong open-source footprint")
    elif p.gh_stars >= 400:
        score += 10
        reasons.append("active open-source work")

    if p.x_followers >= 3000:
        score += 8
        reasons.append("real audience reach")

    # Scholar / research footprint. A "bottom" source : never anchors the
    # candidate, but corroborates technical depth when present. Uses getattr
    # so older Prospect rows (and test fixtures without the field) still
    # work : missing == no signal.
    citations = getattr(p, "scholar_citations", 0) or 0
    if citations >= 500:
        score += 6
        reasons.append("cited research footprint")
    elif citations >= 50:
        score += 3
        reasons.append("published research")

    # --- ICP match --------------------------------------------------------
    # event.seniority is CSV (multi-select). The match threshold is the
    # LOWEST selected rank : "Senior or Staff+" means Senior is acceptable,
    # anything above is too. Accepts legacy single-value strings unchanged.
    selected = [s.strip() for s in (event.seniority or "").split(",") if s.strip()]
    want_ranks = [_SENIORITY_RANK[s] for s in selected if s in _SENIORITY_RANK]
    want = min(want_ranks) if want_ranks else 3
    label = " / ".join(selected) if selected else "Senior"
    have = _SENIORITY_RANK.get(p.seniority, 2)
    if have >= want:
        score += 16
        reasons.append(f"seniority meets the {label} target")
    elif have == want - 1:
        score += 6
        reasons.append("seniority just under target")
    else:
        score -= 8
        reasons.append("seniority below target")

    # --- reachability + corroboration ------------------------------------
    if p.li_resolved:
        score += 8
        reasons.append("contact resolved")
    else:
        score -= 4
        reasons.append("no resolved contact")

    if len((p.sources or "").split(",")) >= 2:
        score += 6
        reasons.append("corroborated across sources")

    score = max(0, min(100, score))
    reason = "; ".join(reasons)
    return score, (reason[:1].upper() + reason[1:] + ".") if reason else "No signal."


def floating_threshold(scores: list[int], funnel_target: int) -> int:
    """Lowest fit bar that still yields >= funnel_target candidates, floored."""
    for t in range(95, config.ABS_FLOOR - 1, -1):
        if sum(1 for s in scores if s >= t) >= funnel_target:
            return t
    return config.ABS_FLOOR
