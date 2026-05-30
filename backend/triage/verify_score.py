"""
triage/verify_score.py : Judge B — the evidence auditor.

Judge A (score.py) scores an applicant from the evidence packet. Judge A is a
single Haiku call and can hallucinate: cite evidence not in the packet, lean on
a *rejected* company candidate, ignore a contradiction, or be over-confident on
thin data. Judge B is a SECOND, stronger model (Sonnet) whose only job is to
AUDIT Judge A's output against the same packet — it does NOT rescore from scratch.

This is a verifier, not a vote. Two independent scorers averaged still carry a
hallucination; a critic that checks "is every claim grounded in the packet?"
catches it. The deterministic consolidator (consolidate.py) then turns the audit
into confidence caps / forced review — the LLMs never set the final verdict.

Cost control: verification is EXPENSIVE (Sonnet) so should_verify() gates it to
risky applicants only — manual-review flags, low company/identity confidence,
name collisions, accepts-despite-warnings, near-threshold, or confident-on-thin.

Fail closed: if Judge B errors or returns unparseable output, the result is
audit_pass=False + force_manual_review=True. A broken auditor must never let an
applicant sail through to accept — it routes them to a human instead.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from ..jsonx import extract_json
from .score import _render_packet, ScoreResult
from .recommend import RecommendationOutput


VERIFY_MODEL = os.environ.get("TRIAGE_VERIFY_MODEL", "claude-sonnet-4-6")
VERIFY_MAX_TOKENS = 1100
VERIFY_TIMEOUT_S = float(os.environ.get("TRIAGE_VERIFY_TIMEOUT", "45"))


_VERIFY_CLIENT = None

def _verify_client():
    global _VERIFY_CLIENT
    if _VERIFY_CLIENT is None:
        from anthropic import Anthropic
        _VERIFY_CLIENT = Anthropic(max_retries=2)
    return _VERIFY_CLIENT


_VERIFY_SYSTEM = """You are an evidence AUDITOR for an event-applicant triage system.

A first model ("the scorer") already scored this applicant across 8 dimensions
and wrote why_fit / why_not_fit. You do NOT rescore. Your only job is to audit
whether the scorer's reasoning is GROUNDED in the evidence packet it was given.

You receive:
  1. The EVIDENCE PACKET (the only legitimate source of facts about the applicant):
       - the applicant's own UNVERIFIED claims
       - LinkedIn person evidence
       - a SELECTED company (the reconciler's pick) + its confidence
       - REJECTED company candidates (considered and discarded, with reasons)
       - contradictions, warnings, and a manual-review flag
  2. THE SCORER'S OUTPUT (dimension scores, confidence, why_fit, why_not_fit).

Audit for these failure modes:
  - UNSUPPORTED EVIDENCE: the scorer asserts a fact (a role, a company property,
    a credential, "uses Stripe at scale") that does NOT appear in the packet.
    The applicant's own claims are UNVERIFIED — treating a claim as confirmed
    fact is unsupported unless LinkedIn/website/co-occurrence corroborates it.
  - USED A REJECTED CANDIDATE: the scorer's praise relies on properties of a
    company that was REJECTED, not the SELECTED one (e.g. selected company is a
    healthcare firm but the scorer calls them an "AI voice startup" because a
    rejected same-name candidate was in AI). This is a critical error.
  - MISSED CONTRADICTION: a contradiction or warning in the packet that the
    scorer ignored and that should have lowered the score or confidence.
  - OVERSTATED CONFIDENCE: confidence is high but identity/company confidence is
    low, evidence is thin, or manual_review_required is true.

Then decide:
  - recommended_confidence_cap: if confidence should be capped, the integer cap
    (0-100); null if no cap needed.
  - manual_review_required: true if a human must look before any accept.

IMPORTANT — what is NOT a contradiction (do not flag these):
  - ABSENCE of a web "co-occurrence" page linking the applicant to their company
    (a `no_person_company_cooccurrence` warning). This is NORMAL for most
    founders and small companies; absence of corroboration is not a conflict.
    Only flag a missed contradiction when the packet contains evidence that
    ACTIVELY CONFLICTS with the scorer's claims (e.g. LinkedIn says a different
    company), never for merely-missing corroboration.
  - "medium" company-resolution confidence, by itself. It is already reflected
    in the deterministic confidence floor; it is not a contradiction.
  - Thin evidence in general. Thin ≠ contradicted. If the scorer was appropriately
    cautious given thin evidence, that is correct behavior, not a failure.

Be PRECISE and CONSERVATIVE. Only flag a claim as unsupported if you genuinely
cannot find support in the packet. Do not invent new concerns. If the scorer's
reasoning is well-grounded, return audit_pass=true with empty lists.

OUTPUT
Return ONLY a JSON object. No prose, no markdown fences. Schema:

{
  "audit_pass": true,
  "used_rejected_candidate": false,
  "unsupported_claims": ["..."],
  "missed_contradictions": ["..."],
  "recommended_confidence_cap": null,
  "manual_review_required": false,
  "short_reason": "one sentence"
}"""


@dataclass
class VerifyResult:
    """Outcome of one Judge B audit. Consumed by consolidate.py."""
    audit_pass: bool = True
    used_rejected_candidate: bool = False
    unsupported_claims: list[str] = field(default_factory=list)
    missed_contradictions: list[str] = field(default_factory=list)
    recommended_confidence_cap: Optional[int] = None
    force_manual_review: bool = False
    short_reason: str = ""
    ran: bool = False           # False when verification was skipped (not risky)
    raw_response: str = ""
    error: Optional[str] = None

    @property
    def flagged(self) -> bool:
        """Did the audit surface anything that must lower the verdict?"""
        return (not self.audit_pass or self.used_rejected_candidate
                or bool(self.unsupported_claims) or bool(self.missed_contradictions)
                or self.force_manual_review
                or self.recommended_confidence_cap is not None)

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "audit_pass": self.audit_pass,
            "used_rejected_candidate": self.used_rejected_candidate,
            "unsupported_claims": self.unsupported_claims,
            "missed_contradictions": self.missed_contradictions,
            "recommended_confidence_cap": self.recommended_confidence_cap,
            "force_manual_review": self.force_manual_review,
            "short_reason": self.short_reason,
            "error": self.error,
        }


def _has_name_collision(packet_dict: dict) -> bool:
    """Two+ company candidates share a (normalized) name → ambiguity risk."""
    names = [(c.get("name") or "").strip().lower()
             for c in packet_dict.get("company_candidates", [])]
    names = [n for n in names if n]
    return any(names.count(n) > 1 for n in set(names))


def should_verify(packet_dict: dict, score: ScoreResult,
                  final: RecommendationOutput) -> tuple[bool, list[str]]:
    """Gate Judge B to risky applicants only. Returns (run?, reasons).

    Skipping the clean majority is what keeps the Sonnet cost bounded; we only
    pay for the audit where Judge A is most likely to be wrong or where a wrong
    accept is most expensive."""
    reasons: list[str] = []
    pe = packet_dict.get("person_evidence") or {}

    if packet_dict.get("manual_review_required"):
        reasons.append("manual_review_required")
    # Only "none" (we resolved NO company at all) is a real risk. low/medium are
    # the norm at an early-stage founder mixer — gating on them fires on everyone.
    if (packet_dict.get("company_resolution_confidence") or "none") == "none":
        reasons.append("no_company_resolved")
    if (packet_dict.get("identity_confidence") or "low") == "low":
        reasons.append("low_identity_confidence")
    if _has_name_collision(packet_dict):
        reasons.append("company_name_collision")
    if packet_dict.get("contradictions"):
        reasons.append("contradictions_present")
    if final.recommendation == "accept" and packet_dict.get("warnings"):
        reasons.append("accept_despite_warnings")
    # Confident on thin evidence: the model is sure but we have little to go on.
    if score.confidence > 75 and final.confidence_score < 70:
        reasons.append("confident_on_thin_evidence")

    return (bool(reasons), reasons)


def verify_score(applicant, packet, score: ScoreResult, *,
                 client=None) -> VerifyResult:
    """Run Judge B: audit the scorer's output against the evidence packet.

    Synchronous (run via asyncio.to_thread like score_applicant). Fails CLOSED:
    any API/parse failure yields audit_pass=False + force_manual_review=True."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return VerifyResult(ran=False, error="ANTHROPIC_API_KEY unset")

    packet_block = _render_packet(packet) if (packet is not None
                                              and not packet.is_empty()) else "(no packet)"
    scorer_block = json.dumps({
        "dimension_scores": score.dimension_scores,
        "confidence": score.confidence,
        "why_fit": score.why_fit,
        "why_not_fit": score.why_not_fit,
        "evidence_used": score.evidence_used,
    }, indent=2)
    user_msg = "\n".join([
        "EVIDENCE PACKET", packet_block, "",
        "SCORER'S OUTPUT", scorer_block, "",
        "Audit the scorer's output now. Output JSON only.",
    ])

    try:
        if client is None:
            client = _verify_client()
        resp = client.messages.create(
            model=VERIFY_MODEL,
            max_tokens=VERIFY_MAX_TOKENS,
            timeout=VERIFY_TIMEOUT_S,
            temperature=0,  # deterministic audit — same packet → same verdict
            system=[{"type": "text", "text": _VERIFY_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return VerifyResult(ran=True, audit_pass=False, force_manual_review=True,
                            short_reason=f"verifier error: {type(exc).__name__}",
                            error=f"{type(exc).__name__}: {exc}")

    text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = extract_json(text)
    if not parsed or not isinstance(parsed, dict):
        # Unparseable audit → fail closed to human review.
        return VerifyResult(ran=True, audit_pass=False, force_manual_review=True,
                            short_reason="verifier returned unparseable output",
                            raw_response=text, error="no parseable JSON")

    return _coerce_verify(parsed, text)


def _coerce_verify(parsed: dict, raw: str) -> VerifyResult:
    """Defensively turn the auditor's JSON into a VerifyResult. Missing/odd
    fields default to the SAFE (more skeptical) side."""
    def _strlist(key: str) -> list[str]:
        v = parsed.get(key)
        return [str(x) for x in v if x] if isinstance(v, list) else []

    cap = parsed.get("recommended_confidence_cap")
    try:
        cap = max(0, min(100, int(cap))) if cap is not None else None
    except (TypeError, ValueError):
        cap = None

    return VerifyResult(
        ran=True,
        audit_pass=bool(parsed.get("audit_pass", False)),
        used_rejected_candidate=bool(parsed.get("used_rejected_candidate", False)),
        unsupported_claims=_strlist("unsupported_claims"),
        missed_contradictions=_strlist("missed_contradictions"),
        recommended_confidence_cap=cap,
        force_manual_review=bool(parsed.get("manual_review_required", False)),
        short_reason=str(parsed.get("short_reason") or "").strip(),
        raw_response=raw,
    )
