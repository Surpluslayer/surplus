"""
triage/score.py : per-applicant Haiku scoring.

Takes:
  - the applicant's CSV fields + raw_application_data
  - optional Exa enrichment (LinkedIn snippet, company snippet)
  - the per-event Rubric synthesized in rubric.py

Produces an ApplicantEvaluation row. The LLM emits dimension scores +
reasoning; the deterministic recommend.py module turns those into the
final fit/confidence/recommendation.

Cost target : ~$0.005 per applicant. Latency : ~2-4s per applicant.
At Verci scale (200 applicants in parallel, concurrency 10), the whole
batch finishes in ~30-60s + 1 Sonnet rubric synth up front.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from .. import models
from ..jsonx import extract_json
from .recommend import finalize, DEFAULT_WEIGHTS
from .rubric import Rubric


SCORE_MODEL = os.environ.get("TRIAGE_SCORE_MODEL", "claude-haiku-4-5-20251001")
SCORE_MAX_TOKENS = 1200
SCORE_CONCURRENCY = 10


_SCORE_SYSTEM = """You score one applicant against a per-event rubric.

INPUT
You'll receive:
  - the rubric (8 dimensions, each with a weight and scoring guidance)
  - the applicant's submitted application fields + custom answers
  - optional enrichment snippets (LinkedIn profile, company website)

JOB
For each of the 8 dimensions, output a 0-100 score that follows the
rubric's guidance for that dimension. Then output:
  - one-sentence summary of the applicant
  - why_fit : 1-2 sentences on what's strong, with concrete evidence
  - why_not_fit : 1-2 sentences on gaps or concerns, with concrete evidence
  - evidence_used : list of short strings naming the data you leaned on
                    (e.g. 'CSV: company=Acme.ai', 'LinkedIn: Staff Eng at...')
  - missing_info : list of fields whose absence reduces confidence
  - archetype : one of founder | operator | engineer | creator | investor |
                researcher | student | service_provider | community_member | other
  - confidence : 0-100, your read on how SURE you are based on the evidence
                 (not how strong the fit is : that's the dimension scores).
                 Low when answers are sparse / contradictory / can't be verified.

GROUND RULES
  - NEVER invent specifics. If you say "they spoke at PyCon," it must be in
    the input. The enrichment snippet is the ONLY source for non-CSV claims.
  - Apply the rubric's hard_gates literally. If the applicant violates a
    hard gate, cap sponsor_fit + event_fit at 30.
  - Apply the rubric's anti-fit guidance literally. If the applicant matches
    an anti-fit category, the rubric tells you what to do (usually cap at 30).
  - confidence reflects EVIDENCE, not fit. A confidently-rejected applicant
    has high confidence + low fit; an unclear applicant has low confidence.
  - Be specific in why_fit / why_not_fit. 'They seem interesting' = bad.
    'B2B SaaS at $40k MRR per their application + Stripe is in their stack' = good.

OUTPUT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "dimension_scores": {
    "sponsor_fit": 0-100,
    "event_fit": 0-100,
    "role_relevance": 0-100,
    "company_relevance": 0-100,
    "stage_relevance": 0-100,
    "seriousness_legitimacy": 0-100,
    "room_value": 0-100,
    "application_quality": 0-100
  },
  "confidence": 0-100,
  "archetype": "founder",
  "one_sentence_summary": "...",
  "why_fit": "...",
  "why_not_fit": "...",
  "evidence_used": ["...", "..."],
  "missing_info": ["...", "..."],
  "suggested_review_action": "..."
}"""


@dataclass
class ScoreResult:
    """In-memory shape of one scoring call's output, before persistence."""
    dimension_scores: dict[str, int]
    confidence: int
    archetype: str
    one_sentence_summary: str
    why_fit: str
    why_not_fit: str
    evidence_used: list[str]
    missing_info: list[str]
    suggested_review_action: str
    raw_response: str = ""
    error: Optional[str] = None


def _build_user_message(applicant, enrichment, rubric: Rubric) -> str:
    parts = ["RUBRIC", rubric.as_json(), ""]
    parts += ["APPLICANT", json.dumps({
        "name": applicant.name,
        "email": applicant.email,
        "role": applicant.role,
        "company": applicant.company,
        "website": applicant.website,
        "linkedin_url": applicant.linkedin_url,
        "raw_application_data": _safe_json_load(applicant.raw_application_data),
    }, indent=2), ""]
    if enrichment and not enrichment.is_empty():
        parts += ["ENRICHMENT (from Exa)", json.dumps(enrichment.as_dict(), indent=2), ""]
    parts.append("Score this applicant. Output JSON now.")
    return "\n".join(parts)


def _safe_json_load(s: str | None) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


_VALID_ARCHETYPES: frozenset[str] = frozenset({
    "founder", "operator", "engineer", "creator", "investor",
    "researcher", "student", "service_provider", "community_member", "other",
})


def _coerce(parsed: Optional[dict], raw: str, error: Optional[str] = None) -> ScoreResult:
    """Defensively turn whatever the model returned into a ScoreResult.
    Unknown archetypes coerce to 'other'; missing fields default safely."""
    if not parsed:
        return ScoreResult(
            dimension_scores={n: 0 for n in DEFAULT_WEIGHTS},
            confidence=0, archetype="other",
            one_sentence_summary="",
            why_fit="", why_not_fit="",
            evidence_used=[], missing_info=[],
            suggested_review_action="(scoring failed)",
            raw_response=raw, error=error or "no parseable JSON",
        )
    dims_in = parsed.get("dimension_scores") or {}
    dims = {}
    for name in DEFAULT_WEIGHTS:
        v = dims_in.get(name, 0)
        try:
            dims[name] = max(0, min(100, int(v)))
        except (ValueError, TypeError):
            dims[name] = 0
    arch = str(parsed.get("archetype") or "other").strip().lower()
    if arch not in _VALID_ARCHETYPES:
        arch = "other"
    try:
        conf = max(0, min(100, int(parsed.get("confidence") or 0)))
    except (ValueError, TypeError):
        conf = 0
    return ScoreResult(
        dimension_scores=dims,
        confidence=conf,
        archetype=arch,
        one_sentence_summary=str(parsed.get("one_sentence_summary") or "").strip(),
        why_fit=str(parsed.get("why_fit") or "").strip(),
        why_not_fit=str(parsed.get("why_not_fit") or "").strip(),
        evidence_used=[str(x) for x in (parsed.get("evidence_used") or []) if x],
        missing_info=[str(x) for x in (parsed.get("missing_info") or []) if x],
        suggested_review_action=str(parsed.get("suggested_review_action") or "").strip(),
        raw_response=raw, error=error,
    )


def score_applicant(applicant: models.Applicant, rubric: Rubric,
                   enrichment=None, *, client=None) -> ScoreResult:
    """One Haiku call per applicant. Synchronous because we run many in
    parallel via asyncio.to_thread in evaluate_all()."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return _coerce(None, "", error="ANTHROPIC_API_KEY unset")

    user_msg = _build_user_message(applicant, enrichment, rubric)
    try:
        if client is None:
            from anthropic import Anthropic
            client = Anthropic()
        t0 = time.time()
        resp = client.messages.create(
            model=SCORE_MODEL,
            max_tokens=SCORE_MAX_TOKENS,
            timeout=15,
            system=[{"type": "text", "text": _SCORE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": "{"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return _coerce(None, "", error=f"{type(exc).__name__}: {exc}")

    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full = "{" + "\n".join(text_chunks)
    parsed = extract_json(full)
    return _coerce(parsed, full)


def persist_evaluation(db, applicant: models.Applicant, event_id: int,
                      score: ScoreResult, rubric: Rubric) -> models.ApplicantEvaluation:
    """Write (or update) the ApplicantEvaluation row for this applicant.

    finalize() combines deterministic confidence_floor with the LLM's
    self-rated confidence : LLM can only LOWER, never raise. fit_score is
    the weighted sum of dimensions per the rubric's weights.
    """
    final = finalize(applicant, score.dimension_scores,
                     llm_confidence=score.confidence,
                     weights=rubric.weights())

    existing = applicant.evaluation
    if existing is None:
        existing = models.ApplicantEvaluation(
            applicant_id=applicant.id, event_id=event_id,
        )
        db.add(existing)

    existing.fit_score = final.fit_score
    existing.confidence_score = final.confidence_score
    existing.recommendation = final.recommendation
    existing.archetype = score.archetype
    for name, value in score.dimension_scores.items():
        setattr(existing, name, value)
    existing.one_sentence_summary = score.one_sentence_summary
    existing.why_fit = score.why_fit
    existing.why_not_fit = score.why_not_fit
    existing.evidence_used = json.dumps(score.evidence_used)
    existing.missing_info = json.dumps(score.missing_info)
    existing.suggested_review_action = score.suggested_review_action
    existing.model_version = SCORE_MODEL
    return existing


async def evaluate_all(db, event: models.Event, rubric: Rubric) -> dict:
    """Score every applicant on this event in parallel, persist results.

    Returns a summary {total, scored, failed} so callers (or the polling
    endpoint) can render progress. Fires Exa enrichment per-applicant too.
    """
    from .enrich import enrich_applicant
    applicants = list(event.applicants)
    if not applicants:
        return {"total": 0, "scored": 0, "failed": 0}

    scored = 0
    failed = 0
    sem = asyncio.Semaphore(SCORE_CONCURRENCY)

    async def _one(a: models.Applicant):
        nonlocal scored, failed
        async with sem:
            try:
                enrichment = await asyncio.to_thread(enrich_applicant, a)
                if enrichment and not enrichment.is_empty():
                    a.enrichment_data = json.dumps(enrichment.as_dict())
                result = await asyncio.to_thread(
                    score_applicant, a, rubric, enrichment,
                )
                if result.error:
                    failed += 1
                else:
                    scored += 1
                persist_evaluation(db, a, event.id, result, rubric)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [triage.score] {a.id} ({a.name}): "
                      f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*[_one(a) for a in applicants], return_exceptions=True)
    db.commit()
    return {"total": len(applicants), "scored": scored, "failed": failed}
