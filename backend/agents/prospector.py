"""
agents/prospector.py — stage 02, concurrent fan-out + ICP gate.

`prospect()` calls every source adapter at once with asyncio.gather, then
merges their partial records on `identity`. The result is a list of plain
dicts — one per unique person — carrying whatever fields the sources between
them could resolve.

When ANTHROPIC_API_KEY is set, every merged candidate then passes through
`llm.judge_relevance()`. The LLM gatekeeper sees the full merged profile
+ the ICP and emits a binary verdict. Non-relevant candidates are dropped
before they ever hit the database, so downstream stages (scorer, outreach,
matcher) only ever see ICP-aligned people.

In mock mode (no API key) the relevance gate is skipped — the mock pool is
already hand-curated, so re-filtering would just churn the demo.
"""
from __future__ import annotations
import asyncio
import os

from . import llm
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


def _relevance_concurrency() -> int:
    """How many judge_relevance calls to run in parallel."""
    try:
        return max(1, int(os.environ.get("PROSPECTING_JUDGE_CONCURRENCY", "4")))
    except ValueError:
        return 4


async def _judge_all(candidates: list[dict], icp: dict) -> list[dict]:
    """Run the LLM gate over every candidate; keep the relevant ones."""
    sem = asyncio.Semaphore(_relevance_concurrency())

    async def _one(c: dict) -> tuple[dict, bool, str]:
        async with sem:
            relevant, reason = await asyncio.to_thread(llm.judge_relevance, c, icp)
            return c, relevant, reason

    results = await asyncio.gather(*(_one(c) for c in candidates))
    kept: list[dict] = []
    for c, relevant, reason in results:
        if relevant:
            # Surfaced for visibility in seed/log output; the field is not
            # persisted to the DB (no migration), but compose() doesn't need
            # it — the LLM-extracted offers/seeks/works_on already carry the
            # personalization payload through.
            c["llm_verdict"] = reason
            kept.append(c)
        else:
            print(f"  [llm] dropped {c.get('name', c.get('identity'))}: {reason}")
    return kept


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

    if llm.llm_available() and out:
        out = await _judge_all(out, icp)
    return out
