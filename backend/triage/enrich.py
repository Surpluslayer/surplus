"""
triage/enrich.py : per-applicant enrichment via Exa.

Best-effort lookups :
  - LinkedIn URL  -> profile page snippet  (verifies claimed role/company)
  - company name  -> company website snippet (verifies the business is real
                     + catches anti-fit categories like 'photography studio')

Failures are silent : enrichment is supplementary signal for the scorer,
never load-bearing. If Exa is down or rate-limited, scoring still runs on
CSV data alone.
"""
from __future__ import annotations
from dataclasses import dataclass

from ..agents import exa as exa_agent


@dataclass(frozen=True)
class ApplicantEnrichment:
    """Compact enrichment payload : just what the scorer needs to ground
    its decision. Truncated so each applicant's prompt stays bounded."""
    linkedin_snippet: str = ""
    company_snippet: str = ""
    company_url_from_search: str = ""

    def as_dict(self) -> dict:
        return {
            "linkedin_snippet": self.linkedin_snippet,
            "company_snippet": self.company_snippet,
            "company_url_from_search": self.company_url_from_search,
        }

    def is_empty(self) -> bool:
        return not (self.linkedin_snippet or self.company_snippet)


def enrich_applicant(applicant) -> ApplicantEnrichment:
    """One Exa /contents call per available URL. Accepts an ORM Applicant
    or any object with linkedin_url / website / company attrs."""
    if not exa_agent.exa_available():
        return ApplicantEnrichment()

    linkedin_url = (getattr(applicant, "linkedin_url", None) or "").strip()
    website = (getattr(applicant, "website", None) or "").strip()
    company = (getattr(applicant, "company", None) or "").strip()

    linkedin_snippet = ""
    if linkedin_url:
        linkedin_snippet = exa_agent.fetch_url_snippet(linkedin_url)

    company_snippet = ""
    company_url_resolved = ""
    if website:
        # Operator-supplied company URL : just fetch it.
        company_snippet = exa_agent.fetch_url_snippet(website)
        company_url_resolved = website if company_snippet else ""
    elif company:
        # No URL provided : search for the company name + grab the top
        # result's snippet. Less precise but better than nothing.
        company_snippet, company_url_resolved = _search_company(company)

    return ApplicantEnrichment(
        linkedin_snippet=linkedin_snippet,
        company_snippet=company_snippet,
        company_url_from_search=company_url_resolved,
    )


def _search_company(name: str) -> tuple[str, str]:
    """Search Exa for the company name, return (snippet, resolved_url) of
    the top result. Returns ('', '') on failure."""
    if not exa_agent.exa_available() or not name:
        return "", ""
    try:
        import httpx
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                "https://api.exa.ai/search",
                headers={
                    "x-api-key": exa_agent._api_key(),
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={
                    "query": f"{name} company website",
                    "type": "neural",
                    "category": "company",
                    "numResults": 1,
                    "contents": {"text": True},
                },
            )
    except Exception:
        return "", ""
    if resp.status_code >= 400:
        return "", ""
    try:
        data = resp.json()
    except Exception:
        return "", ""
    results = data.get("results") or []
    if not results:
        return "", ""
    top = results[0]
    return (top.get("text") or "").strip()[:1500], top.get("url") or ""
