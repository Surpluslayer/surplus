"""Direct GitHub public-API client. No LLM, no scraping.

For a given username, fetches user profile + their public repos, derives
top repos by stars and primary languages, and writes structured fields
that map onto EnrichedPerson.github_* fields.

Rate limits:
  Unauthenticated:  60 req/hour
  With GITHUB_TOKEN: 5000 req/hour

Each user costs 2 API calls (user + repos). With a token you can enrich
~2500 users/hour serially; concurrency multiplies wall-clock speed.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.matching.shared import cache as _cache


GITHUB_API = "https://api.github.com"
CACHE_NAMESPACE = "github"
CACHE_VERSION = "v1"

# Repos to pull per user. 100 is GitHub's per-page max; we'll sort client-side.
REPOS_PER_USER = 100
# How many of the top-starred repos to return in the structured output
TOP_REPOS_RETURNED = 10


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "event-match/0.1",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _read_cache(username: str) -> Optional[dict[str, Any]]:
    return _cache.get(CACHE_NAMESPACE, CACHE_VERSION, username.lower())


def _write_cache(username: str, data: dict[str, Any]) -> None:
    _cache.put(CACHE_NAMESPACE, data, CACHE_VERSION, username.lower())


async def fetch_profile(
    username: str,
    client: httpx.AsyncClient,
    *,
    use_cache: bool = True,
) -> Optional[dict[str, Any]]:
    """Fetch a single GitHub profile + repos + derived languages.

    Returns None if the user doesn't exist (404) or we got rate-limited
    irrecoverably. Other transient errors are caught and logged inline.
    """
    username = (username or "").strip()
    if not username:
        return None

    if use_cache:
        cached = _read_cache(username)
        if cached is not None:
            return cached

    try:
        user_resp = await client.get(f"{GITHUB_API}/users/{username}", headers=_headers())
        if user_resp.status_code == 404:
            return None
        if user_resp.status_code == 403:
            # Rate limited — surface it so the caller can throttle
            remaining = user_resp.headers.get("X-RateLimit-Remaining", "?")
            reset = user_resp.headers.get("X-RateLimit-Reset", "?")
            raise RuntimeError(
                f"GitHub rate limit hit for {username}. "
                f"Remaining={remaining} ResetAt={reset}. Set GITHUB_TOKEN to raise the cap."
            )
        user_resp.raise_for_status()
        user = user_resp.json()

        repos_resp = await client.get(
            f"{GITHUB_API}/users/{username}/repos",
            params={"per_page": REPOS_PER_USER, "sort": "updated", "type": "owner"},
            headers=_headers(),
        )
        repos_resp.raise_for_status()
        repos = repos_resp.json()
    except RuntimeError:
        raise
    except Exception as e:
        # Network blip, malformed JSON, etc. — return None, let the batch continue.
        print(f"[github] {username}: {e!r}")
        return None

    profile = _shape_profile(user, repos)
    if use_cache:
        _write_cache(username, profile)
    return profile


def _shape_profile(user: dict[str, Any], repos: list[dict[str, Any]]) -> dict[str, Any]:
    """Reshape the raw GitHub responses into our flat structured form."""
    # Filter out forks — usually not signal of someone's actual work
    own_repos = [r for r in repos if not r.get("fork")]

    # Top repos by stars
    top_repos = sorted(own_repos, key=lambda r: r.get("stargazers_count", 0), reverse=True)
    top = [
        {
            "name": r.get("name", ""),
            "description": r.get("description") or "",
            "stars": r.get("stargazers_count", 0),
            "language": r.get("language") or "",
            "topics": r.get("topics") or [],
            "url": r.get("html_url", ""),
            "updated_at": r.get("updated_at", ""),
        }
        for r in top_repos[:TOP_REPOS_RETURNED]
    ]

    # Aggregate primary language across ALL non-fork repos (not just top)
    lang_counter: Counter = Counter()
    for r in own_repos:
        lang = r.get("language")
        if lang:
            lang_counter[lang] += 1
    languages = dict(lang_counter.most_common(15))

    # Topic tags across top repos — useful for domain extraction later
    topic_counter: Counter = Counter()
    for r in top_repos[:20]:
        for t in (r.get("topics") or []):
            topic_counter[t] += 1
    top_topics = [t for t, _ in topic_counter.most_common(20)]

    return {
        "username": user.get("login", ""),
        "name": user.get("name") or "",
        "bio": user.get("bio") or "",
        "company": user.get("company") or "",
        "location": user.get("location") or "",
        "blog": user.get("blog") or "",
        "twitter_username": user.get("twitter_username") or "",
        "followers": user.get("followers", 0),
        "following": user.get("following", 0),
        "public_repos": user.get("public_repos", 0),
        "created_at": user.get("created_at", ""),
        "languages": languages,
        "top_repos": top,
        "top_topics": top_topics,
    }


async def batch_fetch(
    usernames: list[str],
    *,
    concurrency: int = 20,
    use_cache: bool = True,
) -> dict[str, Optional[dict[str, Any]]]:
    """Fetch many profiles concurrently. Returns {username: profile_or_None}.

    Auto-deduplicates and skips empty usernames. Honors a semaphore so we
    don't burst past the rate limit even when caller asks for high concurrency.
    """
    usernames = [u for u in {(u or "").strip() for u in usernames} if u]
    if not usernames:
        return {}

    sem = asyncio.Semaphore(concurrency)
    results: dict[str, Optional[dict[str, Any]]] = {}

    timeout = httpx.Timeout(15.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def one(u: str) -> None:
            async with sem:
                try:
                    results[u] = await fetch_profile(u, client, use_cache=use_cache)
                except RuntimeError as e:
                    # Rate-limit error from fetch_profile — stop the batch.
                    print(f"[github] aborting batch: {e}")
                    results[u] = None

        await asyncio.gather(*(one(u) for u in usernames))

    return results
