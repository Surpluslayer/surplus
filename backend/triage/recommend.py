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

Thresholds are per-event: the rubric synthesizer sets them based on
event format (casual mixer vs formal dinner vs invite-only). They're
stored on the Rubric and passed into finalize(). The module-level
DEFAULT_* constants are used only when rubric synthesis fails.
"""
from __future__ import annotations
from dataclasses import dataclass, field
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

# Default thresholds — used only when rubric synthesis fails.
# Calibrated for a mid-formality sponsored event. The rubric synthesizer
# sets per-event thresholds based on event format; these are the fallback.
DEFAULT_ACCEPT_FIT_MIN      = 75
DEFAULT_ACCEPT_CONFIDENCE_MIN = 60
DEFAULT_MAYBE_FIT_MIN       = 55
DEFAULT_MAYBE_CONFIDENCE_MIN  = 50
DEFAULT_REJECT_FIT_MAX      = 40

# Keep old names as aliases so existing imports don't break.
ACCEPT_FIT_MIN      = DEFAULT_ACCEPT_FIT_MIN
ACCEPT_CONFIDENCE_MIN = DEFAULT_ACCEPT_CONFIDENCE_MIN
MAYBE_FIT_MIN       = DEFAULT_MAYBE_FIT_MIN
MAYBE_CONFIDENCE_MIN  = DEFAULT_MAYBE_CONFIDENCE_MIN
REJECT_FIT_MAX      = DEFAULT_REJECT_FIT_MAX


@dataclass(frozen=True)
class Thresholds:
    """Accept / maybe / reject cutoffs for one event.

    All values 0-100. The rubric synthesizer outputs these based on
    event format; finalize() passes them through to recommendation_from().
    """
    accept_fit_min:       int = DEFAULT_ACCEPT_FIT_MIN
    accept_confidence_min: int = DEFAULT_ACCEPT_CONFIDENCE_MIN
    maybe_fit_min:        int = DEFAULT_MAYBE_FIT_MIN
    maybe_confidence_min:  int = DEFAULT_MAYBE_CONFIDENCE_MIN
    reject_fit_max:       int = DEFAULT_REJECT_FIT_MAX

    @classmethod
    def default(cls) -> "Thresholds":
        return cls()

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        """Parse from the rubric JSON. Unknown keys ignored; missing keys
        fall back to defaults so partial rubric output still works."""
        def _int(key: str, default: int) -> int:
            try:
                return max(0, min(100, int(d.get(key, default))))
            except (TypeError, ValueError):
                return default
        return cls(
            accept_fit_min=_int("accept_fit_min", DEFAULT_ACCEPT_FIT_MIN),
            accept_confidence_min=_int("accept_confidence_min", DEFAULT_ACCEPT_CONFIDENCE_MIN),
            maybe_fit_min=_int("maybe_fit_min", DEFAULT_MAYBE_FIT_MIN),
            maybe_confidence_min=_int("maybe_confidence_min", DEFAULT_MAYBE_CONFIDENCE_MIN),
            reject_fit_max=_int("reject_fit_max", DEFAULT_REJECT_FIT_MAX),
        )

    def as_dict(self) -> dict:
        return {
            "accept_fit_min": self.accept_fit_min,
            "accept_confidence_min": self.accept_confidence_min,
            "maybe_fit_min": self.maybe_fit_min,
            "maybe_confidence_min": self.maybe_confidence_min,
            "reject_fit_max": self.reject_fit_max,
        }


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
    for f in ("name", "email", "role", "company"):
        if _get(f):
            score += 10
    # LinkedIn URL + website are higher-value because they're verifiable
    if _get("linkedin_url"):
        score += 15
    if _get("website"):
        score += 10
    # Raw application data : long-form answers are evidence of seriousness
    raw = _get("raw_application_data")
    if raw and raw not in ("{}", "[]"):
        if len(raw) > 200:
            score += 15
        elif len(raw) > 50:
            score += 8
    return min(100, score)


def confidence_floor_breakdown(applicant) -> str:
    """Human-readable explanation of why the confidence floor is what it is.

    Returns a compact string for the CSV 'evidence_summary' column so
    reviewers can instantly see what data was available and what was missing.

    Example: 'name✓ email✓ role✓ company✓ linkedin✓ website✗ answers✗ → 65'
    """
    def _get(name: str) -> str:
        if isinstance(applicant, dict):
            v = applicant.get(name)
        else:
            v = getattr(applicant, name, None)
        return (v or "").strip() if isinstance(v, str) else (str(v).strip() if v else "")

    parts = []
    score = 0
    for f in ("name", "email", "role", "company"):
        if _get(f):
            parts.append(f"{f}✓(+10)")
            score += 10
        else:
            parts.append(f"{f}✗")

    if _get("linkedin_url"):
        parts.append("linkedin✓(+15)")
        score += 15
    else:
        parts.append("linkedin✗")

    if _get("website"):
        parts.append("website✓(+10)")
        score += 10
    else:
        parts.append("website✗")

    raw = _get("raw_application_data")
    if raw and raw not in ("{}", "[]"):
        if len(raw) > 200:
            parts.append("answers✓(+15)")
            score += 15
        elif len(raw) > 50:
            parts.append("answers~(+8)")
            score += 8
        else:
            parts.append("answers✗")
    else:
        parts.append("answers✗")

    floor = min(100, score)
    return " | ".join(parts) + f" → floor={floor}"


def fit_from_dimensions(dimension_scores: dict[str, int],
                       weights: Optional[dict[str, float]] = None) -> int:
    """Weighted sum of the 8 dimension scores -> overall fit_score 0-100."""
    w = weights or DEFAULT_WEIGHTS
    total_weight = sum(w.values()) or 1.0
    raw = sum(int(dimension_scores.get(name, 0)) * weight
              for name, weight in w.items())
    return max(0, min(100, round(raw / total_weight)))


def apply_archetype_priority(
    fit_score: int,
    archetype: str,
    *,
    founder_corroborated: bool = False,
    policy: Optional[dict] = None,
) -> tuple[int, list[str]]:
    """Deterministic, DATA-DRIVEN fit nudge based on the applicant's archetype
    and the event's priority policy (carried on triage_config). Event-agnostic:
    with no policy this is a pure no-op, so the generic engine is unchanged.

    Why this exists: prose in the rubric ('prioritize founders, down-weight
    investors') is soft guidance the LLM can ignore — a well-credentialed VC at a
    famous fund can still out-score a scrappy founder on raw dimensions. This makes
    the operator's priority STRUCTURAL: a deterministic post-adjustment the model
    can't wash out.

    policy shape (all keys optional)::

        {
          "boost": {"founder": 10},   # +N fit for these archetypes
          "cap":   {"investor": 70},  # ceiling fit for these archetypes
          "require_corroboration_for_boost": true  # founders need a real
                                                    # company tie to earn the boost
        }

    The founder boost is gated on `founder_corroborated` (e.g. a self-described
    founder whose email domain matches their claimed company) so we reward real
    builders, never an unverified 'I'm a founder' claim. Returns (adjusted_fit,
    reasons) where reasons explains every adjustment for the audit trail.
    """
    if not policy:
        return fit_score, []
    fit = fit_score
    reasons: list[str] = []
    arch = (archetype or "").strip().lower()
    boost = policy.get("boost") or {}
    cap = policy.get("cap") or {}
    require_corrob = policy.get("require_corroboration_for_boost", True)

    if arch in boost:
        gated_out = (arch == "founder" and require_corrob and not founder_corroborated)
        if gated_out:
            reasons.append("founder boost withheld (no corroborating company/domain)")
        else:
            amt = int(boost[arch])
            fit = min(100, fit + amt)
            reasons.append(f"{arch} priority boost +{amt}")

    if arch in cap:
        ceil = int(cap[arch])
        if fit > ceil:
            reasons.append(f"{arch} fit capped to {ceil} (deprioritized vs founders)")
            fit = ceil

    return max(0, min(100, fit)), reasons


def recommendation_from(fit_score: int, confidence_score: int,
                        thresholds: Optional[Thresholds] = None) -> str:
    """Bucket (fit, confidence) into accept | maybe | reject | needs_review."""
    t = thresholds or Thresholds.default()
    if fit_score >= t.accept_fit_min and confidence_score >= t.accept_confidence_min:
        return "accept"
    if fit_score < t.reject_fit_max:
        return "reject"
    if fit_score >= t.maybe_fit_min and confidence_score >= t.maybe_confidence_min:
        return "maybe"
    # Weak fit that never reached the 'maybe' bar → soft reject, not a human
    # review. An applicant this far below the maybe cutoff with no positive
    # signal is just mediocre; routing them to a human is wasted effort.
    if fit_score < t.maybe_fit_min:
        return "reject"
    # What remains is the genuine borderline: decent fit (>= maybe bar) but
    # confidence too low to call it a 'maybe'. That one a human should see.
    return "needs_review"


def finalize(applicant, dimension_scores: dict[str, int],
             llm_confidence: int,
             weights: Optional[dict[str, float]] = None,
             thresholds: Optional[Thresholds] = None) -> RecommendationOutput:
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
        recommendation=recommendation_from(fit, confidence, thresholds=thresholds),
    )
