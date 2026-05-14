"""
sources/linkedin.py — professional profile + contact resolution.

This is the only adapter that resolves a real contact and the offers/seeks
value vectors. A prospect missing here can still be surfaced by GitHub or X,
but with no resolved contact the scorer will dock them and the outreach agent
has nowhere to send.
"""
from __future__ import annotations
from .base import SourceAdapter, POOL


class LinkedInAdapter(SourceAdapter):
    key = "linkedin"
    latency = 0.50  # third-party resolver — slowest of the three

    async def fetch(self, icp: dict) -> list[dict]:
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
            }
            for p in POOL
        ]
