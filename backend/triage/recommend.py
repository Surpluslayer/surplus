"""
triage/recommend.py : deterministic fit + confidence -> recommendation.

The LLM produces dimension scores and a self-rated confidence; this
module turns those into a single fit_score and recommendation using
fixed rules. Keeping the final mapping out of the LLM means:

  - same inputs -> same recommendation, always
  - operator can predict where the cutoffs are
  - easy to tune thresholds without re-prompting

Confidence is a HYBRID:
  confidence_floor = deterministic from data completeness
  confidence_llm   = model's self-rated confidence
  final            = min(floor, llm)   <- LLM can only LOWER

So a sparse application never gets 'high confidence' even if Claude is
bullish, and an inconsistent application can have confidence pulled
down by the model.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# Dimension weights : sum to 1.0. The rubric step generates per-event
# weights, but we keep these as a defensive default if rubric synthesis
# fails. Conservative: weight sponsor_fit + role_relevance heavily because
# that's the question the operator really cares about.
DEFAULT_WEIGHTS: dict[str, float] = {
    "sponsor_fit": 0.25,
    "event_fit": 0.10,
    "role_relevance": 0.15,
    "company_relevance": 0.12,
    "stage_relevance": 0.08,
    "seriousness_legitimacy": 0.10,
    "room_value": 0.10,
    "application_quality": 0.10,
}

# Recommendation thresholds. Operators can tune these later via config.
ACCEPT_FIT_MIN = 75
ACCEPT_CONFIDENCE_MIN = 60
MAYBE_FIT_MIN = 55
MAYBE_CONFIDENCE_MIN = 50
REJECT_FIT_MAX = 40

VALID_RECOMMENDATIONS: tuple[str, ...] = (
    "accept", "maybe", "reject", "needs_review",
)


@dataclass(frozen=True)
class RecommendationOutput:
    fit_score: int
    confidence_score: int
    recommendation: str


def compute_confidence_floor(applicant) -> int:
    """How much evidence do we have on this applicant?

    Each non-empty canonical field contributes points; LinkedIn URL +
    website carry more weight because they're verifiable. Caps at 100.

    Accepts either an ORM Applicant or a dict with the same fields.
    """
    def _get(name: str) -> str:
        if isinstance(applicant, dict):
            v = applicant.get(name)
        else:
            v = getattr(applicant, name, None)
        return (v or "").strip() if isinstance(v, str) else (str(v).strip() if v else "")

    score = 0
    # Canonical fields contribute 10 each : 6 fields * 10 = max 60 here
    for field in ("name", "email", "role", "company"):
        if _get(field):
            score += 10
    # LinkedIn URL + website are higher-value because they're verifiable
    if _get("linkedin_url"):
        score += 15
    if _get("website"):
        score += 10
    # Raw application data : long-form answers are evidence of seriousness
    raw = _get("raw_application_data")
    if raw and raw not in ("{}", "[]"):
        # Estimate richness by length : a real application has substantive
        # answers; rejected rows are mostly empty or trivial.
        if len(raw) > 200:
            score += 15
        elif len(raw) > 50:
            score += 8
    return min(100, score)


def fit_from_dimensions(dimension_scores: dict[str, int],
                       weights: Optional[dict[str, float]] = None) -> int:
    """Weighted sum of the 8 dimension scores -> overall fit_score 0-100.

    Missing dimensions are treated as 0 (not skipped) : keeps the scale
    consistent so a partially-scored applicant doesn't accidentally rank
    higher than a fully-scored one.
    """
    w = weights or DEFAULT_WEIGHTS
    total_weight = sum(w.values()) or 1.0
    raw = sum(int(dimension_scores.get(name, 0)) * weight
              for name, weight in w.items())
    return max(0, min(100, round(raw / total_weight)))


def recommendation_from(fit_score: int, confidence_score: int) -> str:
    """Bucket (fit, confidence) into accept | maybe | reject | needs_review."""
    if fit_score >= ACCEPT_FIT_MIN and confidence_score >= ACCEPT_CONFIDENCE_MIN:
        return "accept"
    if fit_score < REJECT_FIT_MAX:
        return "reject"
    if fit_score >= MAYBE_FIT_MIN and confidence_score >= MAYBE_CONFIDENCE_MIN:
        return "maybe"
    return "needs_review"


def finalize(applicant, dimension_scores: dict[str, int],
             llm_confidence: int,
             weights: Optional[dict[str, float]] = None) -> RecommendationOutput:
    """Combine LLM output + deterministic floor into the final triplet.

    The LLM can only LOWER confidence : if the data is rich (high floor)
    but the model thinks signals are inconsistent, confidence drops. If
    the data is thin (low floor), the floor is the ceiling no matter how
    bullish the model is.
    """
    fit = fit_from_dimensions(dimension_scores, weights=weights)
    floor = compute_confidence_floor(applicant)
    confidence = min(floor, max(0, min(100, int(llm_confidence))))
    return RecommendationOutput(
        fit_score=fit,
        confidence_score=confidence,
        recommendation=recommendation_from(fit, confidence),
    )
