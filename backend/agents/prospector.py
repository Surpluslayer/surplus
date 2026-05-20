"""
agents/prospector.py : stage 02, concurrent fan-out + ICP gate.

`prospect()` calls every source adapter at once with asyncio.gather, then
merges their partial records on `identity`. The result is a list of plain
dicts : one per unique person : carrying whatever fields the sources between
them could resolve.

When ANTHROPIC_API_KEY is set, every merged candidate then passes through
`llm.judge_relevance()`. The LLM gatekeeper sees the full merged profile
+ the ICP and emits a binary verdict. Non-relevant candidates are dropped
before they ever hit the database, so downstream stages (scorer, outreach,
matcher) only ever see ICP-aligned people.

In mock mode (no API key) the relevance gate is skipped : the mock pool is
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
# : fresh data per deploy).
#
# Tunables:
#   PROSPECTING_CACHE_TTL : seconds, default 3600 (1h). Set 0 to disable.
#   `force_fresh=True` passed to prospect() bypasses the cache for one call.
_PROSPECT_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("PROSPECTING_CACHE_TTL", "3600")))
    except ValueError:
        return 3600


def _adapter_timeout() -> float:
    """Per-adapter wall-clock cap on discovery search.

    30s default : Exa neural search typically completes in 2-5s, so 30s
    is generous headroom. Historical context: this was 120s when the
    pipeline used Anthropic's web_search_20260209 (consistently 60-90s
    per call). Exa is faster by an order of magnitude, so the old cap
    was just dead latency on every failure path.
    """
    try:
        return max(5.0, float(os.environ.get("PROSPECTING_ADAPTER_TIMEOUT", "30")))
    except ValueError:
        return 30.0


def _judge_timeout() -> float:
    """Wall-clock cap for the batched judge call.

    Fail-open on timeout (keep all candidates). 6s default : enough room
    for Haiku to actually finish emitting verdicts on a full batch. A
    short-circuiting timeout means we silently bypass the ICP gate, which
    surfaces wrong-person matches in the UI.
    """
    # Bumped to 30s default : Railway -> Anthropic round-trips routinely
    # need 6-12s for batched judge calls of 50 candidates. 6s was forcing
    # constant fail-open ("kept all candidates"), which bypassed the ICP
    # gate and surfaced bad matches like designers for "ML engineer" ICPs.
    try:
        return max(2.0, float(os.environ.get("PROSPECTING_JUDGE_TIMEOUT", "30")))
    except ValueError:
        return 30.0


def _icp_cache_key(icp: dict) -> str:
    # Only the fields the LLM / Exa search actually conditions on. Sorted for
    # stable bytes. `city` is included so a re-run with a different location
    # doesn't return the previous city's cached pool.
    return json.dumps(
        {k: icp.get(k) for k in ("role", "seniority", "co_stage", "city")},
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
    "scholar_citations": 0,
}


async def _judge_all(candidates: list[dict], icp: dict) -> list[dict]:
    """Run the LLM gate over every candidate; keep the relevant ones.

    Uses `judge_relevance_batch` : a single Haiku call that emits a
    verdict per candidate. Wrapped in asyncio.wait_for so a slow Haiku
    response can't pin the whole /prospect call; on timeout we keep
    every surfaced candidate (fail-open here : discovery already
    self-filters, the judge is a second pass).
    """
    timeout = _judge_timeout()
    try:
        verdicts = await asyncio.wait_for(
            asyncio.to_thread(llm.judge_relevance_batch, candidates, icp),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        print(f"  [llm] judge_relevance_batch exceeded {timeout}s : keeping all candidates")
        return candidates
    kept: list[dict] = []
    for c in candidates:
        relevant, reason = verdicts.get(c["identity"], (False, "no verdict emitted"))
        if relevant:
            # Surfaced for visibility in seed/log output; the field is not
            # persisted to the DB (no migration), but compose() doesn't need
            # it : the LLM-extracted offers/seeks/works_on already carry the
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
        a_start = time.time()
        try:
            result = await asyncio.wait_for(adapter.fetch(icp), timeout=timeout)
            print(f"  [adapter] {adapter.key} → {len(result)} candidates in {time.time() - a_start:.1f}s")
            return result
        except asyncio.TimeoutError:
            print(f"  [adapter] {adapter.key} exceeded {timeout}s : skipped")
            return []
        except Exception as exc:  # noqa: BLE001
            print(f"  [adapter] {adapter.key} crashed: {type(exc).__name__}: {exc}")
            return []

    discover_start = time.time()
    batches = await asyncio.gather(*(_bounded(a) for a in adapters))
    print(f"  [prospect] all adapters completed in {time.time() - discover_start:.1f}s")

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
    dropped_no_linkedin: list[str] = []
    for rec in merged.values():
        rec["sources"] = ",".join(sorted(rec["sources"]))
        for default_key, default_val in _DEFAULTS.items():
            rec.setdefault(default_key, default_val)
        rec["li_resolved"] = bool(rec.pop("contact_resolved", False))

        # LinkedIn URL is the primary driver : without it we can't do
        # outreach at all, so the candidate is noise. GitHub stars and X
        # follower counts are kept as supplementary signal only when they
        # arrived attached to a LinkedIn-anchored record (handles matched
        # at merge time). Discovery via GitHub/X alone, without a LinkedIn
        # handle that matched, gets dropped here.
        if not rec.get("linkedin_url"):
            dropped_no_linkedin.append(rec.get("name") or rec["identity"])
            continue
        out.append(rec)

    if dropped_no_linkedin:
        print(f"  [prospect] dropped {len(dropped_no_linkedin)} candidate(s) "
              f"with no LinkedIn URL: {dropped_no_linkedin[:5]}"
              f"{'…' if len(dropped_no_linkedin) > 5 else ''}")

    # Run the judge unconditionally : it's the ICP gatekeeper that drops
    # off-topic candidates, not just cross-source dedup. Skipping it for
    # single-source runs (a prior optimization) let through wrong-person
    # matches because LinkedIn alone surfaces noise the judge would have
    # caught.
    if llm.llm_available() and out:
        judge_start = time.time()
        out = await _judge_all(out, icp)
        print(f"  [prospect] judge step took {time.time() - judge_start:.1f}s  "
              f"({len(out)} survived)")

    # Only cache non-empty results : caching an empty pool would lock in
    # a transient LLM blip for the full TTL and give the operator a
    # permanently broken event until redeploy.
    if ttl and out:
        _PROSPECT_CACHE[cache_key] = (time.time(), copy.deepcopy(out))
        print(f"  [prospect] cache MISS for {cache_key} : stored {len(out)} candidates")
    elif not out:
        print(f"  [prospect] empty pool for {cache_key} : NOT caching")
    return out
