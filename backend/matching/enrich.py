"""LLM-driven profile enrichment.

For each person, makes ONE Claude call (Sonnet 4.6 with the server-side
web_search tool) to fetch their X profile + LinkedIn preview, then merges
with the already-fetched GitHub profile to produce an EnrichedPerson.

Cost / time per person:
  ~$0.05-0.10 (Sonnet input/output + web search uses)
  ~15-30 seconds
  cached by person_id forever after first run

Concurrency capped by ENRICH_CONCURRENCY env var (default 20).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from anthropic import AsyncAnthropic

from backend.jsonx import extract_json as _extract_json
from backend.matching.schema import Person, EnrichedPerson
from backend.matching.github import fetch_profile as github_fetch_profile
from backend.matching.shared import cache as _cache


# ---- Config ----

MODEL = os.environ.get("ENRICH_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 3000
WEB_SEARCH_MAX_USES = 2  # was 4. Halving roughly halves per-person latency.
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "enrich_system.md"
CACHE_NAMESPACE = "enrich"
CACHE_VERSION = "v1"


# ---- Prompts ----

# Cache the system prompt at module load — it's ~3KB of static text we'll
# send with every enrich call. Anthropic prompt caching gives 90% discount
# on cached tokens after the first hit.
_SYSTEM_PROMPT_CACHE: Optional[str] = None

def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = SYSTEM_PROMPT_PATH.read_text()
    return _SYSTEM_PROMPT_CACHE


def _format_github_summary(gh: Optional[dict[str, Any]]) -> str:
    """One-paragraph human summary of GitHub for the prompt context."""
    if not gh:
        return "(no GitHub data — username missing or fetch failed)"
    parts = [
        f"  username: {gh.get('username', '')}",
        f"  bio: {gh.get('bio') or '(empty)'}",
        f"  location: {gh.get('location') or '(empty)'}",
        f"  followers: {gh.get('followers', 0)}  public_repos: {gh.get('public_repos', 0)}",
        f"  languages: {gh.get('languages', {})}",
        f"  top_topics: {gh.get('top_topics', [])}",
        "  top_repos:",
    ]
    for r in gh.get("top_repos", [])[:8]:
        desc = (r.get("description") or "")[:80]
        parts.append(f"    - {r.get('name', '')} (★{r.get('stars', 0)}, {r.get('language', '')}): {desc}")
    return "\n".join(parts)


def _build_user_message(person: Person, github_profile: Optional[dict[str, Any]]) -> str:
    """Build the per-person user message. System prompt is sent separately (cached)."""
    role_title = person.role or person.title or "(unknown)"
    return (
        "Research this person and return the JSON object per the schema in your system prompt.\n\n"
        "## Input\n\n"
        f"Name:               {person.name}\n"
        f"Role / Title:       {role_title}\n"
        f"Company:            {person.company or '(unknown)'}\n"
        f"LinkedIn URL:       {person.linkedin_url or '(none)'}\n"
        f"X handle:           {person.x_handle or '(none)'}\n"
        f"GitHub (fetched):\n{_format_github_summary(github_profile)}\n"
    )


# ---- Cache (shared namespace-keyed cache; mirrors event-v1's convention) ----

def _read_cache(person_id: str) -> Optional[dict[str, Any]]:
    # Reads disabled during the demo phase — every upload runs fresh through
    # the live LLM+web_search pipeline. Re-enable with ENRICH_CACHE_READS=1.
    if os.environ.get("ENRICH_CACHE_READS", "").lower() not in {"1", "true", "yes"}:
        return None
    return _cache.get(CACHE_NAMESPACE, CACHE_VERSION, MODEL, person_id)


def _write_cache(person_id: str, data: dict[str, Any]) -> None:
    # Writes disabled during the demo phase so the cache never builds up;
    # every viewer sees genuinely fresh enrichment. Re-enable with
    # ENRICH_CACHE_WRITES=1.
    if os.environ.get("ENRICH_CACHE_WRITES", "").lower() not in {"1", "true", "yes"}:
        return
    _cache.put(CACHE_NAMESPACE, data, CACHE_VERSION, MODEL, person_id)


# ---- LLM call ----

async def _call_claude(
    user_message: str,
    *,
    client: AsyncAnthropic,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Single Claude call with web_search enabled. Returns (parsed_json, telemetry).

    Uses prompt caching on the system block: first call writes the cache
    (~25% cost overhead), subsequent calls within 5min read at 90% discount.
    """
    t0 = time.time()
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": _load_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_SEARCH_MAX_USES,
            }],
            messages=[
                {"role": "user", "content": user_message},
                # Prefill the assistant turn with "{" so the model MUST continue
                # with JSON. Haiku is otherwise too chatty when uncertain.
                {"role": "assistant", "content": "{"},
            ],
        )
    except Exception as e:
        return None, {"error": repr(e), "elapsed_s": round(time.time() - t0, 2)}

    elapsed = round(time.time() - t0, 2)

    # Collect text blocks (the model may interleave with tool_use blocks).
    # We prefilled with "{", so prepend it back when reconstructing.
    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full_text = "{" + "\n".join(text_chunks)
    parsed = _extract_json(full_text)

    usage = resp.usage
    telemetry = {
        "model": MODEL,
        "elapsed_s": elapsed,
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        "stop_reason": resp.stop_reason,
        "json_parse_ok": parsed is not None,
        "raw_text_len": len(full_text),
    }
    return parsed, telemetry


# ---- Per-person orchestration ----

async def enrich_person(
    person: Person,
    *,
    anthropic_client: AsyncAnthropic,
    http_client: httpx.AsyncClient,
    use_cache: bool = True,
) -> EnrichedPerson:
    """Enrich one Person to an EnrichedPerson. Always returns; failure shows in status."""
    if use_cache:
        cached = _read_cache(person.id)
        if cached is not None:
            return EnrichedPerson(**cached)

    enriched = EnrichedPerson.from_person(person)

    # 1) GitHub direct API (no LLM)
    gh_profile: Optional[dict[str, Any]] = None
    if person.github_username:
        try:
            gh_profile = await github_fetch_profile(person.github_username, http_client)
            if gh_profile:
                enriched.github_languages = gh_profile.get("languages", {})
                enriched.github_top_repos = gh_profile.get("top_repos", [])
                enriched.github_followers = gh_profile.get("followers", 0)
                enriched.github_public_repos = gh_profile.get("public_repos", 0)
                enriched.enrichment_sources["github"] = "ok"
            else:
                enriched.enrichment_sources["github"] = "failed"
        except Exception as e:
            enriched.enrichment_sources["github"] = "failed"
            enriched.enrichment_errors.append(f"github: {e!r}")
    else:
        enriched.enrichment_sources["github"] = "skipped"

    # 2) Claude + web_search for X + LinkedIn
    if not (person.x_handle or person.linkedin_url):
        # Nothing to LLM-enrich; mark and return
        enriched.enrichment_sources["x"] = "skipped"
        enriched.enrichment_sources["linkedin"] = "skipped"
        enriched.enrichment_status = "partial" if gh_profile else "failed"
        enriched.enriched_at = datetime.now(timezone.utc).isoformat()
        _write_cache(person.id, enriched.to_dict())
        return enriched

    user_message = _build_user_message(person, gh_profile)
    parsed, telemetry = await _call_claude(user_message, client=anthropic_client)

    if parsed is None:
        enriched.enrichment_sources.setdefault("x", "failed")
        enriched.enrichment_sources.setdefault("linkedin", "failed")
        enriched.enrichment_errors.append(f"llm: {telemetry.get('error', 'parse failed')}")
        enriched.enrichment_status = "partial" if gh_profile else "failed"
    else:
        # Merge LLM output onto enriched person
        for field in (
            "roles_history", "tech_stack", "domains", "conviction_themes",
            "x_recent_post_themes", "previous_experiences", "bio_text",
            "x_bio", "linkedin_headline", "linkedin_about",
            "explicit_asks", "mentor_signals", "city",
        ):
            if field in parsed and parsed[field] is not None:
                setattr(enriched, field, parsed[field])
        # Source statuses — LLM tells us what worked
        llm_sources = parsed.get("enrichment_sources", {}) or {}
        for k in ("x", "linkedin"):
            if k in llm_sources:
                enriched.enrichment_sources[k] = llm_sources[k]
            else:
                enriched.enrichment_sources.setdefault(k, "unknown")
        llm_errors = parsed.get("enrichment_errors", []) or []
        if isinstance(llm_errors, list):
            enriched.enrichment_errors.extend(llm_errors)
        # Roll up overall status based on OUTPUT richness, not source coverage.
        # LinkedIn (HTTP 999) and X (HTTP 402) almost always fail their direct
        # fetches in 2026, but Claude triangulates from Crunchbase / personal
        # sites / press / GitHub so the *output* can still be excellent.
        # What matters for matching is whether we extracted useful signal.
        rich_signals = 0
        if (enriched.bio_text or "").strip():
            rich_signals += 1
        if len(enriched.domains) >= 2:
            rich_signals += 1
        if enriched.tech_stack or enriched.roles_history:
            rich_signals += 1
        if enriched.conviction_themes or enriched.previous_experiences:
            rich_signals += 1

        if rich_signals >= 3:
            enriched.enrichment_status = "ok"
        elif rich_signals >= 1:
            enriched.enrichment_status = "partial"
        else:
            enriched.enrichment_status = "failed"

    enriched.enriched_at = datetime.now(timezone.utc).isoformat()
    # Don't cache catastrophic failures (e.g. Anthropic credit-balance errors)
    # otherwise we permanently poison the cache with empty profiles.
    catastrophic = (
        enriched.enrichment_status == "failed"
        and not enriched.bio_text
        and not enriched.domains
        and not enriched.tech_stack
        and not enriched.roles_history
    )
    if not catastrophic:
        _write_cache(person.id, enriched.to_dict())
    return enriched


# ---- Batch orchestration ----

ProgressCallback = Callable[[str, EnrichedPerson, dict[str, Any]], Awaitable[None]]
# (event_type, person, meta) — events: "start", "ok", "error"


async def enrich_batch(
    people: list[Person],
    *,
    concurrency: Optional[int] = None,
    use_cache: bool = True,
    on_progress: Optional[ProgressCallback] = None,
) -> list[EnrichedPerson]:
    """Enrich many people concurrently. Progress callback fires per person."""
    concurrency = concurrency or int(os.environ.get("ENRICH_CONCURRENCY", "20"))

    sem = asyncio.Semaphore(concurrency)
    anthropic_client = AsyncAnthropic()
    timeout = httpx.Timeout(20.0, connect=10.0)
    results: list[Optional[EnrichedPerson]] = [None] * len(people)

    async with httpx.AsyncClient(timeout=timeout) as http_client:
        async def one(i: int, p: Person) -> None:
            async with sem:
                if on_progress:
                    await on_progress("start", EnrichedPerson.from_person(p), {})
                try:
                    enr = await enrich_person(
                        p,
                        anthropic_client=anthropic_client,
                        http_client=http_client,
                        use_cache=use_cache,
                    )
                    results[i] = enr
                    if on_progress:
                        await on_progress("ok", enr, {"status": enr.enrichment_status})
                except Exception as e:
                    enr = EnrichedPerson.from_person(p)
                    enr.enrichment_status = "failed"
                    enr.enrichment_errors.append(f"orchestrator: {e!r}")
                    results[i] = enr
                    if on_progress:
                        await on_progress("error", enr, {"error": repr(e)})

        await asyncio.gather(*(one(i, p) for i, p in enumerate(people)))

    return [r for r in results if r is not None]
