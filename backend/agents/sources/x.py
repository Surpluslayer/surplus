"""
sources/x.py — reach signal.

Two modes:
  - LLM mode (when ANTHROPIC_API_KEY is set): Claude + web_search finds X
    accounts whose follower counts and bios match the ICP.
  - Mock mode: reads from prospect_pool.json.
"""
from __future__ import annotations
import asyncio

from .base import SourceAdapter, POOL
from .. import llm

# below this, there's not enough of an audience for X to be a useful signal
MIN_FOLLOWERS = 100


class XAdapter(SourceAdapter):
    key = "x"
    latency = 0.35  # paid API, rate-limited (mock mode only)

    async def fetch(self, icp: dict) -> list[dict]:
        if llm.llm_available():
            return await asyncio.to_thread(self._fetch_via_llm, icp)
        await self._delay()
        return [
            {
                "identity": p["identity"],
                "name": p["name"],
                "source": self.key,
                "x_followers": p["x_followers"],
            }
            for p in POOL
            if p["x_followers"] >= MIN_FOLLOWERS
        ]

    def _fetch_via_llm(self, icp: dict) -> list[dict]:
        out: list[dict] = []
        for r in llm.discover_candidates("x", icp):
            followers = int(r.get("x_followers") or 0)
            if followers < MIN_FOLLOWERS:
                continue
            out.append({
                "identity": r["identity"],
                "name": r["name"],
                "source": self.key,
                "x_followers": followers,
            })
        return out
