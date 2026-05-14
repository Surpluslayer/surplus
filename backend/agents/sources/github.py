"""sources/github.py — OSS signal. Sees star counts, domain, and market side."""
from __future__ import annotations
from .base import SourceAdapter, POOL

# below this, the OSS footprint is too thin for GitHub to surface them
MIN_STARS = 50


class GitHubAdapter(SourceAdapter):
    key = "github"
    latency = 0.15  # clean public API — fast

    async def fetch(self, icp: dict) -> list[dict]:
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
