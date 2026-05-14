"""
sources/github.py — OSS signal.

Two modes:
  - LLM mode (when ANTHROPIC_API_KEY is set): Claude + web_search surfaces
    real candidates against the ICP, with star counts pulled from the page
    text. Numbers can be approximate.
  - Mock mode (no key): reads from prospect_pool.json so the demo loop and
    tests still work offline.

Both paths emit the same record shape:
    {identity, name, source, gh_stars, works_on?, side?}
"""
from __future__ import annotations
import asyncio

from .base import SourceAdapter, POOL
from .. import llm

# below this, the OSS footprint is too thin for GitHub to surface them
MIN_STARS = 50


class GitHubAdapter(SourceAdapter):
    key = "github"
    latency = 0.15  # clean public API — fast (mock mode only)

    async def fetch(self, icp: dict) -> list[dict]:
        if llm.llm_available():
            return await asyncio.to_thread(self._fetch_via_llm, icp)
        await self._delay()
        return [
            {
                "identity": p["identity"],
                "name": p["name"],
                "source": self.key,
                "gh_stars": p["gh_stars"],
                "works_on": p["works_on"],
                "side": p["side"],
            }
            for p in POOL
            if p["gh_stars"] >= MIN_STARS
        ]

    def _fetch_via_llm(self, icp: dict) -> list[dict]:
        out: list[dict] = []
        for r in llm.discover_candidates("github", icp):
            stars = int(r.get("gh_stars") or 0)
            if stars < MIN_STARS:
                continue
            entry: dict = {
                "identity": r["identity"],
                "name": r["name"],
                "source": self.key,
                "gh_stars": stars,
            }
            if r.get("works_on"):
                entry["works_on"] = r["works_on"]
            if r.get("side"):
                entry["side"] = r["side"]
            out.append(entry)
        return out
