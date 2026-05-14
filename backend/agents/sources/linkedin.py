"""
sources/linkedin.py — professional profile + contact resolution.

This is the only adapter that resolves a real contact and the offers/seeks
value vectors. A prospect missing here can still be surfaced by GitHub or X,
but with no resolved contact the scorer will dock them and the outreach
agent has nowhere to send.

Two modes:
  - LLM mode (when ANTHROPIC_API_KEY is set): Claude + web_search finds
    LinkedIn profiles matching the ICP. Only emits a candidate when a
    /in/<handle> URL is actually visible (`contact_resolved=true`).
  - Mock mode: reads from prospect_pool.json so the offline demo still
    works.
"""
from __future__ import annotations
import asyncio

from .base import SourceAdapter, POOL
from .. import llm


class LinkedInAdapter(SourceAdapter):
    key = "linkedin"
    latency = 0.50  # third-party resolver — slowest (mock mode only)

    async def fetch(self, icp: dict) -> list[dict]:
        if llm.llm_available():
            return await asyncio.to_thread(self._fetch_via_llm, icp)
        await self._delay()
        return [
            {
                "identity": p["identity"],
                "name": p["name"],
                "source": self.key,
                "role": p["role"],
                "company": p["company"],
                "seniority": p["seniority"],
                "offers": p["offers"],
                "seeks": p["seeks"],
                "contact_resolved": p["contact_resolved"],
                # linkedin_url is optional in the pool — some leads can't be
                # resolved to a profile URL. We let the merge see whatever
                # came back so prospect.linkedin_url ends up None when missing.
                **({"linkedin_url": p["linkedin_url"]} if p.get("linkedin_url") else {}),
            }
            for p in POOL
        ]

    def _fetch_via_llm(self, icp: dict) -> list[dict]:
        out: list[dict] = []
        for r in llm.discover_candidates("linkedin", icp):
            entry: dict = {
                "identity": r["identity"],
                "name": r["name"],
                "source": self.key,
            }
            for key in ("role", "company", "seniority", "offers", "seeks"):
                if r.get(key):
                    entry[key] = r[key]
            url = r.get("linkedin_url")
            if url:
                entry["linkedin_url"] = url
            # contact_resolved defaults to True iff we actually got a URL;
            # the LLM may also explicitly set it.
            entry["contact_resolved"] = bool(r.get("contact_resolved", bool(url)))
            out.append(entry)
        return out
