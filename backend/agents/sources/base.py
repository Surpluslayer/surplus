"""
sources/base.py — the adapter contract.

Every prospect source (GitHub, X, LinkedIn, and whatever you add next) is a
SourceAdapter. The prospector fans them out *concurrently* and merges their
partial records on `identity`. Each adapter only returns the fields it can
actually see — that asymmetry is the point:

    github   -> identity, name, gh_stars, works_on, side
    x        -> identity, name, x_followers
    linkedin -> identity, name, role, company, seniority, offers, seeks,
                contact_resolved

A prospect with no LinkedIn hit has no resolved contact; one in only a single
source scores lower. The merge models real enrichment, not a clean lookup.

To go from mock to real: keep the same `fetch(icp) -> list[dict]` signature,
swap the body for an HTTP call. Nothing downstream changes.
"""
from __future__ import annotations
import abc
import asyncio
import json
from pathlib import Path

_POOL_PATH = Path(__file__).parents[2] / "data" / "prospect_pool.json"
POOL: list[dict] = json.loads(_POOL_PATH.read_text())


class SourceAdapter(abc.ABC):
    """One prospect source. `key` names it; `latency` simulates network cost."""

    key: str = "base"
    latency: float = 0.0

    @abc.abstractmethod
    async def fetch(self, icp: dict) -> list[dict]:
        """Return partial prospect records this source can see for the ICP."""
        ...

    async def _delay(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)
