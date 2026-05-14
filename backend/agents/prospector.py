"""
agents/prospector.py — stage 02, concurrent fan-out.

`prospect()` calls every source adapter at once with asyncio.gather, then
merges their partial records on `identity`. The result is a list of plain
dicts — one per unique person — carrying whatever fields the sources between
them could resolve. The pipeline turns these into Prospect rows.
"""
from __future__ import annotations
import asyncio

from .sources import ALL_ADAPTERS, SourceAdapter

# fields a record may still be missing after the merge, and their defaults
_DEFAULTS = {
    "role": "Unknown",
    "company": "Unknown",
    "seniority": "Mid",
    "side": "Builds",
    "works_on": "general",
    "offers": "",
    "seeks": "",
    "gh_stars": 0,
    "x_followers": 0,
}


async def prospect(icp: dict, adapters: list[SourceAdapter] | None = None) -> list[dict]:
    """Fan out across all source adapters concurrently; merge on identity."""
    adapters = adapters or ALL_ADAPTERS
    batches = await asyncio.gather(*(a.fetch(icp) for a in adapters))

    merged: dict[str, dict] = {}
    for batch in batches:
        for raw in batch:
            ident = raw["identity"]
            rec = merged.setdefault(ident, {"identity": ident, "sources": set()})
            rec["sources"].add(raw.get("source", "?"))
            for key, val in raw.items():
                if key in ("identity", "source"):
                    continue
                rec.setdefault(key, val)  # first source to resolve a field wins

    out: list[dict] = []
    for rec in merged.values():
        rec["sources"] = ",".join(sorted(rec["sources"]))
        for key, default in _DEFAULTS.items():
            rec.setdefault(key, default)
        rec["li_resolved"] = bool(rec.pop("contact_resolved", False))
        out.append(rec)
    return out
