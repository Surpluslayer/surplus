"""
triage/reconcile.py : turn raw evidence + claims into an EvidencePacket.

This is the step that used to live (badly) inside enrich.py. enrich.py now
returns every company candidate it found; this module ranks them deterministically,
selects one (or flags for manual review), and records *why the others were
rejected*. The scorer downstream sees the whole packet — selected AND rejected —
so it can still override when the deterministic pick looks wrong.

The cardinal rule, learned from the Kyndred collision:

    Person-company evidence beats event-theme evidence.

A candidate is NEVER selected just because its description semantically matches
the event ("AI characters" for an AI event). Selection is driven by evidence that
ties THIS applicant to THIS company: their name on the company's page, a domain
they submitted, a work-experience entry, a headline mention. Event-theme fit is
recorded for the scorer but contributes nothing to selection.

Output is deterministic and LLM-free. Reasons are templated from which signals
fired, so the audit trail is reproducible.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from .answers import Claims
from .enrich import CompanyCandidate, PersonEvidence, RawEvidence


# ── Event-theme tagging (advisory only, never decisive) ───────────────────

def _event_keywords(triage_config: dict | None) -> frozenset[str]:
    """Token set describing the event, for advisory event-theme tagging.
    Drawn from goal / ideal-attendee / sponsor — NOT used for selection."""
    if not triage_config:
        return frozenset()
    blob = " ".join(str(triage_config.get(k) or "") for k in (
        "event_goal", "ideal_attendee_profile", "sponsor_name", "event_type"))
    toks = re.sub(r"[^a-z0-9 ]", " ", blob.lower()).split()
    stop = {"the", "and", "for", "with", "who", "are", "you", "your", "this",
            "that", "will", "from", "have", "want", "people", "event", "join"}
    return frozenset(t for t in toks if len(t) >= 4 and t not in stop)


def _tag_event_theme(cand: CompanyCandidate, kw: frozenset[str]) -> None:
    if not kw:
        return
    text = f"{cand.industry} {cand.description}".lower()
    toks = set(re.sub(r"[^a-z0-9 ]", " ", text).split())
    cand.matches_event_theme = len(toks & kw) >= 2


# ── Candidate scoring : person-company evidence only ──────────────────────

def _candidate_score(cand: CompanyCandidate) -> float:
    """Deterministic strength of the applicant↔company link. Event-theme is
    deliberately excluded — it must never drive selection."""
    score = 0.0
    if cand.matches_person_name:        score += 3.0   # name on company page
    if cand.matches_submitted_domain:   score += 3.0   # applicant gave this domain
    if cand.matches_email_domain:       score += 3.0   # email domain = company website
    if cand.matches_work_experience:    score += 2.0   # LinkedIn work history
    if cand.matches_linkedin_headline:  score += 2.0   # headline mention
    # LLM identity match : only set in the no-hard-tie ambiguous zone, where it is
    # the strongest signal available (a reasoned read of the full descriptions).
    # Weighted on par with a soft structural tie so it decisively breaks the
    # website/follower noise that was picking the wrong same-named company, but
    # selection on it alone is capped to medium confidence below.
    if cand.matches_llm_identity:       score += 2.5
    if cand.matches_claimed_company:    score += 1.0   # name-only match (weak)
    if cand.website:                    score += 1.0
    if cand.location:                   score += 0.5
    if cand.employee_count:             score += 0.5
    if cand.follower_count >= 25:       score += 0.5
    # Penalties for the hollow-company shape.
    if "low_follower_count" in cand.warnings:            score -= 1.0
    if "no_website" in cand.warnings:                    score -= 1.0
    if "no_person_company_cooccurrence" in cand.warnings: score -= 1.5
    return score


def _candidate_reason(cand: CompanyCandidate, score: float) -> str:
    pos = []
    if cand.matches_person_name:       pos.append("applicant name appears on company page")
    if cand.matches_submitted_domain:  pos.append("matches applicant-submitted domain")
    if cand.matches_email_domain:      pos.append("matches applicant email domain")
    if cand.matches_work_experience:   pos.append("matches LinkedIn work experience")
    if cand.matches_linkedin_headline: pos.append("matches LinkedIn headline")
    if cand.matches_llm_identity:
        pos.append("LLM identity match"
                   + (f": {cand.llm_identity_reason}" if cand.llm_identity_reason else ""))
    if not pos and cand.matches_claimed_company:
        pos.append("company-name match only")
    base = "; ".join(pos) or "no applicant-company evidence"
    if cand.warnings:
        base += f" (warnings: {', '.join(cand.warnings)})"
    return f"{base} [score={score:.1f}]"


# ── EvidencePacket ────────────────────────────────────────────────────────

@dataclass
class SelectedCompany:
    name: str = ""
    url: str = ""
    source: str = ""
    confidence: str = "low"   # high | medium | low
    reason: str = ""
    # What the company ACTUALLY does, from the fetched homepage / LinkedIn company
    # description. Carried through to the scorer so company_relevance is judged on
    # the real product, not the applicant's self-claimed industry or "AI" headline.
    description: str = ""
    industry: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "source": self.source,
                "confidence": self.confidence, "reason": self.reason,
                "description": self.description, "industry": self.industry}


@dataclass
class EvidencePacket:
    applicant_id: str = ""
    identity: dict = field(default_factory=dict)
    luma_claims: dict = field(default_factory=dict)
    person_evidence: dict = field(default_factory=dict)
    company_candidates: list[dict] = field(default_factory=list)
    selected_company: SelectedCompany | None = None
    rejected_company_candidates: list[dict] = field(default_factory=list)
    identity_confidence: str = "low"   # high | medium | low
    contradictions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "applicant_id": self.applicant_id,
            "identity": self.identity,
            "luma_claims": self.luma_claims,
            "person_evidence": self.person_evidence,
            "company_candidates": self.company_candidates,
            "selected_company": self.selected_company.as_dict() if self.selected_company else None,
            "rejected_company_candidates": self.rejected_company_candidates,
            "identity_confidence": self.identity_confidence,
            "company_resolution_confidence": (
                self.selected_company.confidence if self.selected_company else "none"),
            "contradictions": self.contradictions,
            "missing": self.missing,
            "warnings": self.warnings,
            "manual_review_required": self.manual_review_required,
            "manual_review_reason": self.manual_review_reason,
        }

    def is_empty(self) -> bool:
        return not (self.person_evidence.get("linkedin_profile_found")
                    or self.company_candidates)


def _identity_confidence(person: PersonEvidence, claims: Claims) -> tuple[str, list[str]]:
    missing: list[str] = []
    if not person.found:
        missing.append("linkedin_profile")
        return "low", missing
    if not person.matches_name:
        return "low", missing
    if not person.work_experience_found:
        missing.append("linkedin_work_experience")
        return "medium", missing
    return "high", missing


def _detect_contradictions(person: PersonEvidence, claims: Claims,
                           candidates: list[CompanyCandidate],
                           selected: CompanyCandidate | None) -> list[str]:
    out: list[str] = []
    # A same-name collision among candidates is only a LIVE contradiction if we
    # did NOT confidently disambiguate which company is the applicant's. If the
    # selected company is tied to the applicant by hard evidence (their submitted/
    # email domain, their LinkedIn work history, or headline), the collision is
    # already resolved — surfacing it as a contradiction just makes the auditor
    # flag a "missed contradiction" the scorer correctly ignored.
    resolved = selected is not None and (
        selected.matches_submitted_domain
        or selected.matches_email_domain
        or selected.matches_work_experience
        or selected.matches_linkedin_headline)
    if not resolved:
        # Two candidates share a name but look like different companies.
        names = [c.name.lower() for c in candidates if c.name]
        dupes = {n for n in names if names.count(n) > 1}
        for n in dupes:
            same = [c for c in candidates if c.name.lower() == n]
            industries = {c.industry.lower() for c in same if c.industry}
            if len(industries) > 1:
                out.append(
                    f"Multiple companies named '{same[0].name}' with differing "
                    f"industries ({', '.join(sorted(i for i in industries if i))}) "
                    f"— likely a name collision.")
    # NOTE: a LinkedIn headline company that merely *differs in string form* from
    # the selected company is NOT a contradiction — casual headlines ("Genius")
    # vs formal web-resolved names ("Genius HRTech Limited"), people who list a
    # side project, etc. produce constant false positives. A real contradiction
    # is the headline company matching a DIFFERENT (rejected) candidate, i.e. the
    # evidence actively points elsewhere. That case is already covered by the
    # name-collision logic above. The soft "differs" signal is emitted as a
    # warning by reconcile(), not as a contradiction.
    return out


def reconcile(applicant, claims: Claims, raw: RawEvidence,
              triage_config: dict | None = None) -> EvidencePacket:
    """Build the EvidencePacket from raw evidence. Deterministic."""
    aid = str(getattr(applicant, "id", "") or "")
    person = raw.person
    candidates = list(raw.company_candidates)

    kw = _event_keywords(triage_config)
    for c in candidates:
        _tag_event_theme(c, kw)
        # luma-industry match : does the candidate's industry/desc echo the claim?
        claim_text = f"{claims.claimed_industry} {claims.claimed_project}".lower()
        if claim_text.strip():
            ctoks = set(re.sub(r"[^a-z0-9 ]", " ", claim_text).split())
            ctoks = {t for t in ctoks if len(t) >= 4}
            cand_toks = set(re.sub(r"[^a-z0-9 ]", " ",
                                   f"{c.industry} {c.description}".lower()).split())
            c.matches_luma_industry = len(ctoks & cand_toks) >= 1

    # Rank purely on person-company evidence.
    scored = sorted(candidates, key=_candidate_score, reverse=True)
    selected_cand = scored[0] if scored else None
    top_score = _candidate_score(selected_cand) if selected_cand else 0.0
    runner_score = _candidate_score(scored[1]) if len(scored) > 1 else 0.0

    selected: SelectedCompany | None = None
    if selected_cand and top_score > 0:
        if top_score >= 4.0 and (top_score - runner_score) >= 2.0:
            conf = "high"
        elif top_score >= 2.0:
            conf = "medium"
        else:
            conf = "low"
        # A pick that rests on the LLM identity judgment (no hard/soft structural
        # tie of its own) is a reasoned inference, not a verified link — never let
        # it read as "high". Cap at medium so downstream treats it as corroborated-
        # but-not-confirmed.
        structural_tie = (selected_cand.matches_person_name
                          or selected_cand.matches_submitted_domain
                          or selected_cand.matches_email_domain
                          or selected_cand.matches_work_experience
                          or selected_cand.matches_linkedin_headline)
        if conf == "high" and selected_cand.matches_llm_identity and not structural_tie:
            conf = "medium"
        selected = SelectedCompany(
            name=selected_cand.name,
            url=selected_cand.website or selected_cand.linkedin_url,
            source=selected_cand.source,
            confidence=conf,
            reason=_candidate_reason(selected_cand, top_score),
            description=(selected_cand.description or "").strip()[:600],
            industry=(selected_cand.industry or "").strip(),
        )

    rejected = [
        {"name": c.name, "url": c.website or c.linkedin_url, "source": c.source,
         "reason": _candidate_reason(c, _candidate_score(c)),
         "description": (c.description or "").strip()[:300]}
        for c in scored if c is not selected_cand
    ]

    identity_conf, missing = _identity_confidence(person, claims)
    contradictions = _detect_contradictions(person, claims, candidates, selected_cand)

    warnings: list[str] = list(person.warnings)
    if claims.red_flags:
        warnings.extend(f"claim:{r}" for r in claims.red_flags)
    # Soft signal (NOT a contradiction): headline company differs in string form
    # from the selected company. Informational only.
    if selected and person.current_company \
            and person.current_company.lower() not in selected.name.lower() \
            and selected.name.lower() not in person.current_company.lower():
        warnings.append(
            f"linkedin_headline_company '{person.current_company}' differs in "
            f"name from selected company '{selected.name}'")

    # ── Manual-review triggers ───────────────────────────────────────────
    review_reasons: list[str] = []
    # A decisive direct tie (name on page, submitted domain, or email domain)
    # resolves a name collision on its own — don't force manual review then.
    strong_direct_tie = selected_cand is not None and (
        selected_cand.matches_person_name
        or selected_cand.matches_submitted_domain
        or selected_cand.matches_email_domain)
    same_name_collision = any("name collision" in c for c in contradictions)
    if same_name_collision and not strong_direct_tie:
        review_reasons.append("multiple companies share the claimed name")
    if selected_cand is not None:
        if not selected_cand.website:
            review_reasons.append("selected company has no website")
        # Absence of a web co-occurrence page is only a manual-review trigger
        # when NOTHING ELSE ties the applicant to the company. If their own
        # LinkedIn (work history / headline) or a submitted/email domain already
        # links them, the co-occurrence article is redundant — its absence is the
        # normal case for most founders, not a red flag. Gating on it blindly was
        # flagging ~60% of applicants for manual review.
        company_tie = (selected_cand.matches_person_name
                       or selected_cand.matches_submitted_domain
                       or selected_cand.matches_email_domain
                       or selected_cand.matches_work_experience
                       or selected_cand.matches_linkedin_headline)
        if ("no_person_company_cooccurrence" in selected_cand.warnings
                and not company_tie):
            review_reasons.append("no applicant↔company tie (no co-occurrence and "
                                  "no LinkedIn/domain link to selected company)")
        if (selected_cand.follower_count and selected_cand.follower_count < 25
                and not selected_cand.website):
            review_reasons.append("selected company is hollow (<25 followers, no website)")
        # Event-theme-only selection : nothing ties the person to it.
        if (selected_cand.matches_event_theme and not (
                selected_cand.matches_person_name or selected_cand.matches_submitted_domain
                or selected_cand.matches_work_experience
                or selected_cand.matches_linkedin_headline)):
            review_reasons.append("company fit rests only on event-theme match, "
                                  "not applicant evidence")
    elif candidates:
        review_reasons.append("company candidates found but none could be resolved")
    if not person.work_experience_found and person.found:
        # A substantial profile (headline + followers) that came back with an
        # empty work-experience array is almost always a LinkedIn soft-throttle
        # (the experience section is stripped under load), NOT a person with no
        # track record. Forcing manual_review here was tanking corroborated
        # founders like Harpriya (a16z-speedrun headline, 2k followers, work=[]).
        # Downgrade to a warning so the scorer treats it as an unverified data
        # gap (lower confidence) rather than disconfirming legitimacy.
        if getattr(person, "work_unreliable", False):
            warnings.append("linkedin work experience missing but profile is "
                            "substantial → likely throttle-stripped, not absent")
        else:
            review_reasons.append("LinkedIn work experience missing")
    # Founder claim with no external support.
    claims_founder = "found" in (claims.claimed_role or "").lower() \
        or "founder" in (person.headline or "").lower()
    has_external = bool(selected) and selected.confidence in ("high", "medium")
    if claims_founder and not has_external:
        review_reasons.append("claims founder but no external source corroborates a company")

    manual = bool(review_reasons)

    return EvidencePacket(
        applicant_id=aid,
        identity={
            "name": getattr(applicant, "name", "") or "",
            "email": getattr(applicant, "email", "") or "",
            "linkedin_url": getattr(applicant, "linkedin_url", "") or "",
        },
        luma_claims=claims.as_dict(),
        person_evidence=person.as_dict(),
        company_candidates=[c.as_dict() for c in candidates],
        selected_company=selected,
        rejected_company_candidates=rejected,
        identity_confidence=identity_conf,
        contradictions=contradictions,
        missing=missing,
        warnings=warnings,
        manual_review_required=manual,
        manual_review_reason="; ".join(review_reasons),
    )
