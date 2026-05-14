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
import copy
import json
import os
import time

from . import llm
from .sources import ALL_ADAPTERS, SourceAdapter


# ── ICP-keyed response cache ────────────────────────────────────────────────
# Web search is the wall-clock bottleneck in LLM mode and it's the same work
# every time when the ICP doesn't change. Iterating on copy / styling / the
# outreach UI shouldn't pay 25s to re-fetch the same candidates. We cache
# prospect()'s output in-memory keyed by a stable hash of the ICP fields
# we actually pass to the model. Cleared on redeploy (which is what you want
# — fresh data per deploy).
#
# Tunables:
#   PROSPECTING_CACHE_TTL — seconds, default 3600 (1h). Set 0 to disable.
#   `force_fresh=True` passed to prospect() bypasses the cache for one call.
_PROSPECT_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("PROSPECTING_CACHE_TTL", "3600")))
    except ValueError:
        return 3600


def _adapter_timeout() -> float:
    """Per-adapter wall-clock cap on web_search.

    120s default. Anthropic's web_search_20260209 is consistently taking
    60-90s per call from Railway EU West, even with max_uses=1. 60s
    caught nothing in prod. Trade off latency for actually-finishing.
    The right long-term fix is to swap web_search for a real search
    backend (Exa) — see PR notes.
    """
    try:
        return max(5.0, float(os.environ.get("PROSPECTING_ADAPTER_TIMEOUT", "120")))
    except ValueError:
        return 120.0


def _judge_timeout() -> float:
    """Wall-clock cap for the batched judge call."""
    try:
        return max(2.0, float(os.environ.get("PROSPECTING_JUDGE_TIMEOUT", "15")))
    except ValueError:
        return 15.0


def _icp_cache_key(icp: dict) -> str:
    # Only the fields the LLM actually conditions on. Sorted for stable bytes.
    return json.dumps(
        {k: icp.get(k) for k in ("role", "seniority", "co_stage")},
        sort_keys=True,
    )

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


async def _judge_all(candidates: list[dict], icp: dict) -> list[dict]:
    """Run the LLM gate over every candidate; keep the relevant ones.

    Uses `judge_relevance_batch` — a single Haiku call that emits a
    verdict per candidate. Wrapped in asyncio.wait_for so a slow Haiku
    response can't pin the whole /prospect call; on timeout we keep
    every surfaced candidate (fail-open here — discovery already
    self-filters, the judge is a second pass).
    """
    timeout = _judge_timeout()
    try:
        verdicts = await asyncio.wait_for(
            asyncio.to_thread(llm.judge_relevance_batch, candidates, icp),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        print(f"  [llm] judge_relevance_batch exceeded {timeout}s — keeping all candidates")
        return candidates
    kept: list[dict] = []
    for c in candidates:
        relevant, reason = verdicts.get(c["identity"], (False, "no verdict emitted"))
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


async def prospect(
    icp: dict,
    adapters: list[SourceAdapter] | None = None,
    force_fresh: bool = False,
) -> list[dict]:
    """Fan out across all source adapters concurrently; merge on identity.

    Memoizes the full result by ICP fingerprint for PROSPECTING_CACHE_TTL
    seconds (default 1h). Subsequent runs against the same ICP return in
    <1s instead of re-running web_search. Pass force_fresh=True to bust
    AND delete any stale entry (so a one-off bad cache value can be
    cleared without restarting the process).
    """
    ttl = _cache_ttl()
    cache_key = _icp_cache_key(icp)
    if force_fresh:
        _PROSPECT_CACHE.pop(cache_key, None)
    elif ttl:
        hit = _PROSPECT_CACHE.get(cache_key)
        if hit and time.time() - hit[0] < ttl:
            age = int(time.time() - hit[0])
            print(f"  [prospect] cache HIT for {cache_key} ({age}s old, {len(hit[1])} candidates)")
            return copy.deepcopy(hit[1])

    adapters = adapters or ALL_ADAPTERS
    timeout = _adapter_timeout()

    async def _bounded(adapter: SourceAdapter) -> list[dict]:
        # Each adapter gets its own wall-clock cap so one stuck call (often
        # Anthropic's web_search going into a multi-minute retry loop on the
        # server side) can't pin the whole pipeline. On timeout we treat that
        # source as "returned nothing" and continue with the others.
        try:
            return await asyncio.wait_for(adapter.fetch(icp), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"  [adapter] {adapter.key} exceeded {timeout}s — skipped")
            return []
        except Exception as exc:  # noqa: BLE001
            print(f"  [adapter] {adapter.key} crashed: {type(exc).__name__}: {exc}")
            return []

    batches = await asyncio.gather(*(_bounded(a) for a in adapters))

    merged: dict[str, dict] = {}
    for batch in batches:
        for raw in batch:
            ident = raw["identity"]
            rec = merged.setdefault(ident, {"identity": ident, "sources": set()})
            rec["sources"].add(raw.get("source", "?"))
            for raw_key, raw_val in raw.items():
                if raw_key in ("identity", "source"):
                    continue
                rec.setdefault(raw_key, raw_val)  # first source to resolve a field wins

    out: list[dict] = []
    for rec in merged.values():
        rec["sources"] = ",".join(sorted(rec["sources"]))
        for default_key, default_val in _DEFAULTS.items():
            rec.setdefault(default_key, default_val)
        rec["li_resolved"] = bool(rec.pop("contact_resolved", False))
        out.append(rec)

    if llm.llm_available() and out:
        out = await _judge_all(out, icp)

    # Only cache non-empty results — caching an empty pool would lock in
    # a transient LLM blip for the full TTL and give the operator a
    # permanently broken event until redeploy.
    if ttl and out:
        _PROSPECT_CACHE[cache_key] = (time.time(), copy.deepcopy(out))
        print(f"  [prospect] cache MISS for {cache_key} — stored {len(out)} candidates")
    elif not out:
        print(f"  [prospect] empty pool for {cache_key} — NOT caching")
    return out
