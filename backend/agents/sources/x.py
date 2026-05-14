"""sources/x.py — reach signal. Sees follower counts only."""
from __future__ import annotations
from .base import SourceAdapter, POOL

# below this, there's not enough of an audience for X to be a useful signal
MIN_FOLLOWERS = 100


class XAdapter(SourceAdapter):
    key = "x"
    latency = 0.35  # paid API, rate-limited — slower

    async def fetch(self, icp: dict) -> list[dict]:
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
