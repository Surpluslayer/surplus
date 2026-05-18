"""
sources/scholar.py : academic / research signal.

This is a "bottom" source : it never anchors a candidate on its own (Google
Scholar profiles don't yield a LinkedIn URL the way Exa's LinkedIn results
do), so it only adds value as supplementary signal attached to a record
that *also* surfaced from LinkedIn / GitHub / X. The merge in prospector.py
takes the union of fields keyed by `identity`, so a Scholar hit on
'maya-rodriguez' just bolts `scholar_citations` onto the existing record.

Two modes:
  - LLM mode (when EXA_API_KEY or ANTHROPIC_API_KEY is set): search
    scholar.google.com (and semanticscholar.org / arxiv.org as fallbacks)
    for researchers matching the ICP. Returns approximate citation counts
    from the result snippet text.
  - Mock mode (no key): reads from prospect_pool.json so the offline demo
    still works.

Record shape: {identity, name, source, scholar_citations, scholar_url?}
"""
from __future__ import annotations
import asyncio

from .base import SourceAdapter, POOL
from .. import llm

# below this, an academic footprint is too thin to be a useful signal
MIN_CITATIONS = 25


class ScholarAdapter(SourceAdapter):
    key = "scholar"
    latency = 0.45  # scraped via search index : roughly LinkedIn-class latency

    async def fetch(self, icp: dict) -> list[dict]:
        if llm.llm_available():
            return await asyncio.to_thread(self._fetch_via_llm, icp)
        await self._delay()
        return [
            {
                "identity": p["identity"],
                "name": p["name"],
                "source": self.key,
                "scholar_citations": p["scholar_citations"],
            }
            for p in POOL
            if p.get("scholar_citations", 0) >= MIN_CITATIONS
        ]

    def _fetch_via_llm(self, icp: dict) -> list[dict]:
        out: list[dict] = []
        for r in llm.discover_candidates("scholar", icp):
            citations = int(r.get("scholar_citations") or 0)
            if citations < MIN_CITATIONS:
                continue
            entry: dict = {
                "identity": r["identity"],
                "name": r["name"],
                "source": self.key,
                "scholar_citations": citations,
            }
            if r.get("scholar_url"):
                entry["scholar_url"] = r["scholar_url"]
            out.append(entry)
        return out
