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
from typing import Optional, TYPE_CHECKING

from .. import models
from ..jsonx import extract_json
from .recommend import finalize, DEFAULT_WEIGHTS
from .rubric import Rubric

if TYPE_CHECKING:  # avoid the score → consolidate → verify_score → score cycle
    from .verify_score import VerifyResult


SCORE_MODEL = os.environ.get("TRIAGE_SCORE_MODEL", "claude-haiku-4-5-20251001")
SCORE_MAX_TOKENS = 1200
SCORE_CONCURRENCY = 25
# Bumped from 15s : Railway -> Anthropic round-trips routinely take 6-12s
# for the TCP/TLS handshake + Haiku response, and fresh-client connection
# storms make it worse. Matches the 30s default we use in compose
# (backend/agents/outreach.py).
SCORE_TIMEOUT_S = float(os.environ.get("TRIAGE_SCORE_TIMEOUT", "30"))


# Module-singleton Anthropic client for score_applicant : same fix as
# compose (PR #93). Per-call Anthropic() instantiation hits Railway's
# egress connection limits and surfaces as APIConnectionError on every
# request, leaving us with all-zero "(scoring failed)" evaluations.
_SCORE_CLIENT = None

def _score_client():
    global _SCORE_CLIENT
    if _SCORE_CLIENT is None:
        from anthropic import Anthropic
        _SCORE_CLIENT = Anthropic(max_retries=2)
    return _SCORE_CLIENT


_SCORE_SYSTEM = """You score one applicant against a per-event rubric.

INPUT
You'll receive:
  - the rubric (8 dimensions, each with a weight and scoring guidance)
  - the applicant's submitted application fields + custom answers
  - an EVIDENCE PACKET assembled from external enrichment, containing:
      * the applicant's own claims (role, company, project, reason for attending)
      * LinkedIn person evidence (headline, work history, name-match)
      * a SELECTED company (the reconciler's best guess) with a confidence level
        AND a description of what that company actually does (from its website /
        company profile) — use this to judge relevance, not the headline
      * REJECTED company candidates — alternatives that were considered and why
        they lost. These exist because company-name collisions are common.
      * contradictions, missing data, warnings, and a manual-review flag

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
    the input. The evidence packet is the ONLY source for non-CSV claims.
  - REASON OVER THE PACKET, don't blindly trust the SELECTED company. The
    reconciler is deterministic and can be wrong. If a REJECTED candidate fits
    the applicant's own claims/headline better than the selected one, say so in
    why_not_fit and score as if the company is uncertain. Company-name matches
    that don't also match the PERSON (no name on the page, no work-history tie)
    are weak — do not treat an event-theme-convenient company as confirmed.
  - Person-company evidence beats event-theme convenience. A company that merely
    looks on-theme for the event, but has no tie to THIS applicant, is NOT
    evidence the applicant belongs.
  - JUDGE company_relevance / sponsor_fit FROM WHAT THE COMPANY ACTUALLY DOES,
    not from the applicant's headline or self-claimed industry. When the packet
    gives a "WHAT THE COMPANY ACTUALLY DOES" description, that product reality is
    authoritative. A headline that says "AI" over a company whose description is a
    marketplace, agency, dev-shop, consultancy, school, ecommerce, real-estate,
    staffing, or other non-AI/non-thesis business does NOT make it on-thesis —
    score company_relevance/sponsor_fit on the description, and call out the
    headline-vs-product mismatch in why_not_fit. Only credit a company as
    on-thesis (e.g. AI/voice/agents for an AI event) when its actual product
    supports it; "AI" appearing only in the headline or self-claim is not enough.
  - If manual_review_required is true OR identity/company confidence is low OR
    there are contradictions, your confidence MUST be ≤ 55.
  - If SELF_TITLED_PROFILE is flagged, the LinkedIn evidence confirms identity
    but NOT a real track record: cap seriousness_legitimacy at 40, and do not
    let LinkedIn alone lift sponsor_fit/company_relevance above 60 unless the
    application answers or a corroborating website independently substantiate the
    company. Your confidence MUST be ≤ 60.
  - If WORK_HISTORY_UNRELIABLE is flagged, the empty work-experience is a DATA
    GAP from a LinkedIn throttle, NOT evidence of an empty career. Do NOT cap
    seriousness_legitimacy for the missing history and do NOT treat it like
    SELF_TITLED. Instead, lower CONFIDENCE (the track record is unverified) while
    still crediting whatever DOES corroborate the applicant — headline, email
    domain matching the company site, follower count, application answers. A
    founder corroborated those ways can still be a confident fit; just keep
    confidence modest (≤ 70) to reflect the unverified work history.
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


def _build_user_message(applicant, packet, rubric: Rubric) -> str:
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
    if packet is not None and not packet.is_empty():
        parts += ["EVIDENCE PACKET", _render_packet(packet), ""]
    parts.append("Score this applicant. Output JSON now.")
    return "\n".join(parts)


def _render_packet(packet) -> str:
    """Human-readable rendering of the EvidencePacket for the scorer. Reads as
    prose so the model reasons over candidates rather than parsing JSON."""
    d = packet.as_dict()
    lines: list[str] = []

    claims = d.get("luma_claims") or {}
    lines.append("Applicant claims (self-reported, UNVERIFIED):")
    for k in ("claimed_role", "claimed_company", "claimed_project",
              "claimed_industry", "claimed_stage", "reason_for_attending",
              "stripe_answer", "creator_answer"):
        v = (claims.get(k) or "").strip()
        if v:
            lines.append(f"  - {k}: {v}")
    if claims.get("red_flags"):
        lines.append(f"  - red_flags: {', '.join(claims['red_flags'])}")

    pe = d.get("person_evidence") or {}
    lines.append("")
    lines.append("LinkedIn person evidence:")
    lines.append(f"  - profile_found: {pe.get('linkedin_profile_found')}, "
                 f"name_match: {pe.get('linkedin_profile_matches_name')}, "
                 f"work_experience_found: {pe.get('linkedin_work_experience_found')}")
    if pe.get("headline"):
        lines.append(f"  - headline: {pe['headline']}")
    if pe.get("work_experience"):
        lines.append(f"  - work_experience: {'; '.join(pe['work_experience'][:4])}")
    if pe.get("linkedin_self_titled"):
        lines.append("  - SELF_TITLED_PROFILE: every work entry is a placeholder "
                     "(job title == company name) and there is no About text. This "
                     "profile confirms the person's NAME but not a verified track "
                     "record — treat as weak legitimacy evidence.")
    if pe.get("linkedin_work_unreliable"):
        lines.append("  - WORK_HISTORY_UNRELIABLE: the profile is clearly "
                     "substantial (real headline + follower count) but its "
                     "work-experience section came back EMPTY — the signature of "
                     "a LinkedIn soft-throttle that strips the experience list, "
                     "NOT a person with no track record. Treat the missing work "
                     "history as an UNVERIFIED DATA GAP: lower your CONFIDENCE, but "
                     "do NOT count it against the applicant's legitimacy. A founder "
                     "corroborated by headline/email-domain/company-site can still "
                     "be a confident fit despite this gap.")

    lines.append("")
    lines.append(f"identity_confidence: {d.get('identity_confidence')} | "
                 f"company_resolution_confidence: {d.get('company_resolution_confidence')}")

    sel = d.get("selected_company")
    lines.append("")
    if sel:
        lines.append(f"SELECTED company: {sel['name']} ({sel.get('url') or 'no url'}) "
                     f"[{sel['confidence']} confidence] — {sel['reason']}")
        if sel.get("industry"):
            lines.append(f"  - industry (from company profile): {sel['industry']}")
        if sel.get("description"):
            lines.append(
                f"  - WHAT THE COMPANY ACTUALLY DOES (from its website/company "
                f"profile, NOT the applicant's headline): {sel['description']}")
    else:
        lines.append("SELECTED company: NONE could be resolved from evidence.")

    rejected = d.get("rejected_company_candidates") or []
    if rejected:
        lines.append("REJECTED company candidates (consider whether one of these "
                     "actually fits the applicant better):")
        for r in rejected[:4]:
            desc = (r.get("description") or "").strip()
            tail = f" — does: {desc}" if desc else ""
            lines.append(f"  - {r['name']} ({r.get('url') or 'no url'}) "
                         f"[{r.get('source')}] — {r['reason']}{tail}")

    if d.get("contradictions"):
        lines.append("")
        lines.append("Contradictions:")
        for c in d["contradictions"]:
            lines.append(f"  - {c}")

    if d.get("warnings"):
        lines.append(f"\nWarnings: {', '.join(d['warnings'][:8])}")
    if d.get("manual_review_required"):
        lines.append(f"\nMANUAL_REVIEW_REQUIRED: {d.get('manual_review_reason')}")

    return "\n".join(lines)


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
                   packet=None, *, client=None) -> ScoreResult:
    """One Haiku call per applicant. Synchronous because we run many in
    parallel via asyncio.to_thread in evaluate_all().

    `packet` is the EvidencePacket from reconcile.reconcile()."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return _coerce(None, "", error="ANTHROPIC_API_KEY unset")

    user_msg = _build_user_message(applicant, packet, rubric)
    try:
        if client is None:
            client = _score_client()
        t0 = time.time()
        resp = client.messages.create(
            model=SCORE_MODEL,
            max_tokens=SCORE_MAX_TOKENS,
            timeout=SCORE_TIMEOUT_S,
            temperature=0,  # deterministic scoring — same evidence → same score
            system=[{"type": "text", "text": _SCORE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return _coerce(None, "", error=f"{type(exc).__name__}: {exc}")

    # Claude 4.x dropped support for assistant-message prefill, so we no
    # longer seed the response with "{". extract_json tolerates preamble
    # and pulls the first complete JSON object from the text.
    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full = "\n".join(text_chunks)
    parsed = extract_json(full)
    return _coerce(parsed, full)


def _founder_domain_corroborated(applicant, packet) -> bool:
    """True when the applicant's email domain matches their claimed company —
    the operator's strongest 'real founder' signal ('they say they founded X and
    have an @X email'). Free-mail domains (gmail etc.) never corroborate. Checks
    the submitted website and the reconciler's selected company URL. Independent
    of LinkedIn, so it survives a throttled/empty work-history pull.
    """
    from .enrich import _email_domain, _domain, _domains_match
    email_dom = _email_domain(getattr(applicant, "email", "") or "")
    if not email_dom:
        return False
    company_domains: list[str] = []
    site = getattr(applicant, "website", "") or ""
    if site:
        company_domains.append(_domain(site))
    if packet is not None and not packet.is_empty():
        sel = (packet.as_dict().get("selected_company") or {})
        if sel.get("url"):
            company_domains.append(_domain(sel["url"]))
    return any(cd and _domains_match(email_dom, cd) for cd in company_domains)


def persist_evaluation(db, applicant: models.Applicant, event_id: int,
                      score: ScoreResult, rubric: Rubric, *,
                      verify: Optional[VerifyResult] = None,
                      founder_corroborated: bool = False,
                      priority_policy: Optional[dict] = None,
                      ) -> models.ApplicantEvaluation:
    """Write (or update) the ApplicantEvaluation row for this applicant.

    consolidate() combines the deterministic confidence_floor with the LLM's
    self-rated confidence (LLM can only LOWER, never raise) AND Judge B's audit
    (when it ran). The auditor can only make the verdict MORE conservative —
    lower confidence, block an accept, force review — never upgrade it. When
    `verify` is None (clean applicant, audit skipped) consolidate() is exactly
    finalize() wrapped, so behaviour is unchanged for the common case.

    `founder_corroborated` / `priority_policy` make the operator's archetype
    priority (boost founders, cap investors) a deterministic fit adjustment —
    see recommend.apply_archetype_priority.
    """
    from .consolidate import consolidate  # lazy : breaks import cycle

    final = consolidate(applicant, score.dimension_scores,
                        llm_confidence=score.confidence,
                        weights=rubric.weights(),
                        thresholds=rubric.thresholds,
                        verify=verify,
                        archetype=score.archetype,
                        founder_corroborated=founder_corroborated,
                        priority_policy=priority_policy)

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
    existing.verifier_ran = final.verifier_ran
    existing.verifier_adjustments = json.dumps(list(final.adjustments))
    existing.verifier_reason = (verify.short_reason if verify is not None else "")
    existing.model_version = SCORE_MODEL
    return existing


async def evaluate_all(db, event: models.Event, rubric: Rubric, *,
                       force_reenrich: bool = False) -> dict:
    """Score every applicant on this event in parallel, persist results.

    Pipeline per applicant: parse claims → enrich (raw candidates) → reconcile
    (EvidencePacket) → score from the packet → audit (Judge B) → consolidate.

    REPRODUCIBILITY: enrichment is the only non-deterministic layer (Unipile/Exa
    return different snippets each call). We enrich ONCE, persist the raw evidence
    on Applicant.enrichment_raw, and on every subsequent run rehydrate that frozen
    raw instead of re-hitting the network — so reconcile + score (both temp=0)
    produce the SAME verdict run-to-run. Pass force_reenrich=True to deliberately
    refresh the evidence (e.g. the applicant updated their LinkedIn).

    The reconciled packet is stored on enrichment_data and is re-derived every
    run, so editing triage_config / the ICP still changes the verdict without a
    re-enrich. An optional per-applicant debug JSON is written when
    TRIAGE_DEBUG_DIR is set.

    Returns a summary {total, scored, failed} so callers (or the polling
    endpoint) can render progress.
    """
    from .answers import parse_claims
    from .disambiguate import disambiguate_company
    from .enrich import enrich_applicant, RawEvidence
    from .reconcile import reconcile
    from .verify_score import should_verify, verify_score

    applicants = list(event.applicants)
    if not applicants:
        return {"total": 0, "scored": 0, "failed": 0}

    triage_config = _safe_json_load(getattr(event, "triage_config", None))
    # Operator's archetype-priority policy (boost founders / cap investors), made
    # structural via consolidate(). Absent → no-op, generic scoring unchanged.
    _priority_policy = (triage_config.get("archetype_priority")
                        if isinstance(triage_config, dict) else None)
    scored = 0
    failed = 0
    sem = asyncio.Semaphore(SCORE_CONCURRENCY)
    # Family A — second-pass recovery. Applicants whose work-experience came back
    # throttle-stripped (work_unreliable) on a FRESH fetch are collected here; we
    # never cache that stripped pull, and after the main pass cools down we re-fetch
    # them at low concurrency to try to recover the real history.
    deferred: list[models.Applicant] = []

    async def _one(a: models.Applicant, *, is_retry: bool = False):
        nonlocal scored, failed
        async with sem:
            try:
                claims = parse_claims(a)
                # Reuse frozen raw enrichment when present (reproducible re-runs);
                # only hit the Unipile/Exa network on first eval or a forced refresh.
                raw = None
                persisted = (getattr(a, "enrichment_raw", "") or "").strip()
                if persisted and not force_reenrich and not is_retry:
                    try:
                        raw = RawEvidence.from_dict(json.loads(persisted))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        raw = None  # corrupt cache → fall through to re-enrich
                fresh_fetch = raw is None
                if raw is None:
                    raw = await enrich_applicant(a, claims)
                    # Do NOT cache a throttle-stripped pull (work_unreliable): it
                    # would freeze the empty work-history forever, so every future
                    # run reconciles the SAME gutted evidence. Leave enrichment_raw
                    # unset so the second pass (and later runs) re-fetch and can
                    # recover the real history once the account cools down.
                    if not raw.is_empty() and not raw.person.work_unreliable:
                        a.enrichment_raw = json.dumps(raw.as_dict())
                    # Queue a freshly-stripped applicant for one cooled-down retry.
                    if (fresh_fetch and not is_retry
                            and raw.person.work_unreliable):
                        deferred.append(a)
                # Company disambiguation : when >=2 same-named candidates and no
                # hard structural tie resolves the winner (the Kyndred case), ask
                # Claude which company is actually THIS person's, identity-only.
                # Runs on fresh enrichment only — a cached pull already carries any
                # prior llm_identity flag, so re-runs stay free + deterministic.
                if fresh_fetch and not raw.is_empty():
                    await asyncio.to_thread(
                        disambiguate_company, claims, raw.person,
                        raw.company_candidates)
                packet = reconcile(a, claims, raw, triage_config)
                if not packet.is_empty():
                    a.enrichment_data = json.dumps(packet.as_dict())
                result = await asyncio.to_thread(
                    score_applicant, a, rubric, packet,
                )
                _write_debug_artifact(a, claims, raw, packet, result)
                # On the second pass these applicants were already counted in the
                # first pass (the row is re-derived/overwritten, not added), so
                # only move the scored/failed tallies on the initial pass.
                if result.error:
                    if not is_retry:
                        failed += 1
                    # Surface the per-applicant error in logs : without this a
                    # silent "(scoring failed)" evaluation row gives no hint
                    # whether it was a network error, a JSON parse, or a key.
                    print(f"  [triage.score] {a.id} ({a.name}): {result.error}")
                elif not is_retry:
                    scored += 1
                # Judge B (evidence auditor) — gated to risky applicants only so
                # the expensive Sonnet call is bounded. We skip it on a scoring
                # error (nothing trustworthy to audit). should_verify needs the
                # provisional finalize() verdict to detect accept-despite-warnings
                # and confident-on-thin; consolidate() then re-derives the final
                # verdict from the audit inside persist_evaluation.
                verify = None
                if not result.error:
                    packet_dict = (packet.as_dict() if (packet is not None
                                   and not packet.is_empty()) else {})
                    provisional = finalize(
                        a, result.dimension_scores,
                        llm_confidence=result.confidence,
                        weights=rubric.weights(),
                        thresholds=rubric.thresholds)
                    run_audit, audit_reasons = should_verify(
                        packet_dict, result, provisional)
                    if run_audit:
                        print(f"  [triage.verify] {a.id} ({a.name}): "
                              f"auditing — {', '.join(audit_reasons)}")
                        verify = await asyncio.to_thread(
                            verify_score, a, packet, result)
                persist_evaluation(
                    db, a, event.id, result, rubric, verify=verify,
                    founder_corroborated=_founder_domain_corroborated(a, packet),
                    priority_policy=_priority_policy)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [triage.score] {a.id} ({a.name}): "
                      f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*[_one(a) for a in applicants], return_exceptions=True)

    # ── Family A : second-pass recovery for throttle-stripped applicants ──────
    # During the main pass these were scored from a soft-throttled 200 (empty
    # work-experience). We didn't cache that pull, so here we let the account pool
    # cool down, then re-enrich them at LOW concurrency (gentler on the account)
    # and re-score. If the history comes back, the cache now holds the good pull
    # and the verdict is grounded in a real track record; if it's still stripped,
    # nothing is cached and the (already conservative) first-pass score stands.
    if deferred and not force_reenrich:
        retry_targets = list({id(a): a for a in deferred}.values())
        cooldown = float(os.environ.get("TRIAGE_RETRY_COOLDOWN_SECS", "20"))
        print(f"  [triage.score] second pass: {len(retry_targets)} throttle-stripped "
              f"applicant(s), cooling down {cooldown:.0f}s before re-enrich")
        await asyncio.sleep(cooldown)
        retry_sem = asyncio.Semaphore(
            int(os.environ.get("TRIAGE_RETRY_CONCURRENCY", "1")))

        async def _retry_one(a: models.Applicant):
            async with retry_sem:
                await _one(a, is_retry=True)

        await asyncio.gather(*[_retry_one(a) for a in retry_targets],
                             return_exceptions=True)

    db.commit()
    return {"total": len(applicants), "scored": scored, "failed": failed}


def _write_debug_artifact(applicant, claims, raw, packet, result) -> None:
    """Dump the full reasoning trail for one applicant to TRIAGE_DEBUG_DIR.

    No-op unless TRIAGE_DEBUG_DIR is set (production has ephemeral disk). This
    is the 'explain what evidence was used AND rejected' artifact."""
    debug_dir = (os.environ.get("TRIAGE_DEBUG_DIR") or "").strip()
    if not debug_dir:
        return
    try:
        os.makedirs(debug_dir, exist_ok=True)
        aid = getattr(applicant, "id", None) or "unknown"
        path = os.path.join(debug_dir, f"applicant_{aid}.json")
        artifact = {
            "applicant_id": aid,
            "name": getattr(applicant, "name", ""),
            "parsed_claims": claims.as_dict(),
            "raw_evidence": raw.as_dict(),
            "evidence_packet": packet.as_dict(),
            "scoring_output": {
                "dimension_scores": result.dimension_scores,
                "confidence": result.confidence,
                "archetype": result.archetype,
                "one_sentence_summary": result.one_sentence_summary,
                "why_fit": result.why_fit,
                "why_not_fit": result.why_not_fit,
                "evidence_used": result.evidence_used,
                "missing_info": result.missing_info,
                "error": result.error,
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        print(f"  [triage.debug] failed to write artifact: {exc}")
