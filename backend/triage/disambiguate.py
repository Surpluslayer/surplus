"""LLM company disambiguation : resolve WHICH candidate the applicant runs.

The problem (canonical case: Brittany / Kyndred). enrich() returns every company
hit as a CompanyCandidate; reconcile() then ranks them on *structural* evidence —
the applicant's name on the company page, a submitted/email domain match, the
company appearing in their LinkedIn work history or headline. Those are strong,
unambiguous ties.

But when the person's own LinkedIn was soft-throttled (work history stripped) and
there's no co-occurrence article, NONE of those ties fire. Two same-named
companies then rank only on website/follower noise — and the bigger, better-SEO'd
namesake ("Kyndred Health", a real website, 4k followers) beats the founder's
actual seed-stage company ("Kyndred", no website yet). The deterministic ranker
picks the wrong one and the founder gets scored against a stranger's company.

This module fills exactly that gap and NOTHING else:

  - It runs ONLY in the ambiguous zone (>=2 candidates, no hard structural tie,
    a real claimed company to match). When a hard tie exists the deterministic
    ranker is already correct, so we don't spend a Haiku call. See
    needs_disambiguation().

  - It judges IDENTITY only — "which of these companies is the one this person
    actually founded / works at," reading the full candidate descriptions,
    industries, and the applicant's claimed role/company/project. It is told
    NOTHING about the event and must NEVER consider desirability or fit (that is
    the scorer's job, downstream). This keeps the engine event-agnostic.

  - On a confident pick it sets `matches_llm_identity=True` + a short reason on
    the chosen candidate. reconcile rewards that flag but CAPS the resulting
    company-resolution confidence at "medium" (a reasoned judgment, not a hard
    tie). On no-confident-pick or any failure it does nothing → the existing
    deterministic ranking stands. Fail-safe by construction.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from ..jsonx import extract_json

if TYPE_CHECKING:  # avoid import cycle at runtime
    from .answers import Claims
    from .enrich import CompanyCandidate, PersonEvidence

DISAMBIG_MODEL = os.environ.get("TRIAGE_DISAMBIG_MODEL", "claude-haiku-4-5-20251001")
DISAMBIG_MAX_TOKENS = int(os.environ.get("TRIAGE_DISAMBIG_MAX_TOKENS", "400"))

# Module-singleton client : per-call Anthropic() instantiation exhausts Railway's
# egress connection budget (same fix as score.py / compose).
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        _CLIENT = Anthropic(max_retries=2)
    return _CLIENT


def _hard_tie(c: "CompanyCandidate") -> bool:
    """A structural, unambiguous applicant<->company link. When one exists the
    deterministic ranker is trustworthy and no LLM call is warranted."""
    return bool(c.matches_person_name or c.matches_submitted_domain
                or c.matches_email_domain)


def needs_disambiguation(claims: "Claims",
                         person: "PersonEvidence",
                         candidates: list["CompanyCandidate"]) -> bool:
    """Gate the (paid) LLM call to the genuinely murky cases only.

    Fire ONLY when every cheap structural signal has failed to resolve a winner:
      - there are >=2 real candidates,
      - NONE of them carries a hard structural tie (name-on-page / domain),
      - a real claimed company exists to match against,
      - the person's own profile didn't already single one out via work history
        or headline (exactly ONE soft-tie candidate = deterministic is fine).
    """
    cands = [c for c in candidates if (c.name or "").strip()]
    if len(cands) < 2:
        return False
    if any(_hard_tie(c) for c in cands):
        return False
    if not (getattr(claims, "claimed_company", "") or "").strip():
        return False
    # The person's LinkedIn already points at exactly one candidate → trust it.
    soft = sum(1 for c in cands
               if c.matches_work_experience or c.matches_linkedin_headline)
    if soft == 1:
        return False
    return True


def _person_block(person: "PersonEvidence") -> str:
    parts = []
    if person.headline:
        parts.append(f"  headline: {person.headline}")
    if person.about:
        parts.append(f"  about: {person.about[:300]}")
    if person.work_companies:
        parts.append(f"  past companies (LinkedIn): {', '.join(person.work_companies)}")
    if not parts:
        parts.append("  (LinkedIn profile thin or unavailable)")
    return "\n".join(parts)


def _candidate_block(i: int, c: "CompanyCandidate") -> str:
    bits = [f"[{i}] {c.name}"]
    if c.industry:
        bits.append(f"industry={c.industry}")
    if c.website:
        bits.append(f"website={c.website}")
    if c.location:
        bits.append(f"location={c.location}")
    if c.follower_count:
        bits.append(f"followers={c.follower_count}")
    if c.employee_count:
        bits.append(f"size={c.employee_count}")
    head = "  " + " | ".join(bits)
    desc = (c.description or "").strip()
    if desc:
        head += f"\n      desc: {desc[:280]}"
    return head


def _build_prompt(claims: "Claims", person: "PersonEvidence",
                  candidates: list["CompanyCandidate"]) -> str:
    cand_text = "\n".join(_candidate_block(i, c) for i, c in enumerate(candidates))
    role = (claims.claimed_role or "").strip() or "(unstated)"
    industry = (claims.claimed_industry or "").strip()
    project = (claims.claimed_project or "").strip()
    claim_extra = ""
    if industry:
        claim_extra += f"\n  claimed industry: {industry}"
    if project:
        claim_extra += f"\n  describes their work as: {project[:240]}"
    return (
        "You are resolving an IDENTITY question, not judging quality or fit.\n"
        "An event applicant says they are at a company. Several companies share "
        "that name or are plausible matches. Decide WHICH ONE is the company this "
        "specific person actually founded or works at — purely from identity "
        "evidence. Do NOT consider whether the company is impressive, well-funded, "
        "or a good event fit; that is judged separately.\n\n"
        f"APPLICANT'S CLAIM:\n"
        f"  claimed role: {role}\n  claimed company: {claims.claimed_company}"
        f"{claim_extra}\n\n"
        f"APPLICANT'S LINKEDIN:\n{_person_block(person)}\n\n"
        f"CANDIDATE COMPANIES:\n{cand_text}\n\n"
        "Pick the single candidate index that is most likely THIS person's "
        "company. Weigh: does the candidate's industry/description match the "
        "applicant's claimed role and work? does its name match the claim more "
        "precisely (an exact-name seed-stage company often beats a larger, "
        "similarly-named but unrelated firm)? does it fit the person's seniority "
        "and field? If no candidate is a credible match for THIS person, return "
        "choice -1.\n\n"
        'Reply ONLY with JSON: {"choice": <index or -1>, '
        '"confidence": "high|medium|low", "reason": "<=20 words"}'
    )


def disambiguate_company(claims: "Claims",
                         person: "PersonEvidence",
                         candidates: list["CompanyCandidate"],
                         *, client=None) -> Optional[dict]:
    """If ambiguous, ask Haiku which candidate is the applicant's real company.

    Mutates the chosen candidate in place (matches_llm_identity=True + reason) and
    returns the parsed LLM verdict dict, or None if the call was skipped/failed/
    inconclusive. NEVER raises — any error degrades to the deterministic ranking.
    """
    if not needs_disambiguation(claims, person, candidates):
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return None
    cands = list(candidates)
    try:
        cli = client or _client()
        resp = cli.messages.create(
            model=DISAMBIG_MODEL,
            max_tokens=DISAMBIG_MAX_TOKENS,
            messages=[{"role": "user",
                       "content": _build_prompt(claims, person, cands)}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        verdict = extract_json(text)
    except Exception as exc:  # noqa: BLE001 — disambiguation is best-effort
        print(f"  [triage.disambig] failed ({type(exc).__name__}: {exc}) → "
              f"deterministic ranking stands")
        return None

    if not verdict:
        return None
    try:
        choice = int(verdict.get("choice", -1))
    except (TypeError, ValueError):
        choice = -1
    conf = str(verdict.get("confidence") or "").lower()
    reason = str(verdict.get("reason") or "").strip()[:160]
    # Only act on a credible, in-range pick. A "low" confidence pick is not worth
    # overriding the deterministic ranking with — leave it alone.
    if not (0 <= choice < len(cands)) or conf not in ("high", "medium"):
        print(f"  [triage.disambig] inconclusive (choice={choice}, conf={conf!r}) "
              f"→ deterministic ranking stands")
        return verdict
    chosen = cands[choice]
    chosen.matches_llm_identity = True
    chosen.llm_identity_reason = reason or "LLM identity match"
    print(f"  [triage.disambig] chose [{choice}] {chosen.name!r} "
          f"(conf={conf}) — {reason}")
    return verdict
