"""
agents/llm.py — Claude-driven prospecting helpers.

Three operations, all gated by ANTHROPIC_API_KEY:

  discover_candidates(source, icp)      web-search-driven discovery per source
  judge_relevance(candidate, icp)       LLM gatekeeper — ICP match verdict

`llm_available()` returns True only when the SDK is installed AND a key is
set in the environment. Callers must check it first and fall back to the
mock pool when False (so seed/tests still work offline).

Design notes:
- Model is claude-opus-4-7 (sampling params and budget_tokens are removed
  on Opus 4.7 — adaptive thinking only; the system prompt is the only
  steering knob).
- The system prompt is stable and gets a cache_control breakpoint — every
  per-candidate judge call reads the cache instead of paying full price.
- The discover_candidates call returns ONE tool_use block per candidate
  (via `emit_candidate`), so we don't have to parse free text.
- The relevance verdict is forced via tool_choice to keep the response
  shape predictable.
"""
from __future__ import annotations
import json
import os
from typing import Optional

try:
    import anthropic
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


MODEL = "claude-opus-4-7"
# Judge is a binary classifier ("does this profile match the ICP, yes/no").
# Haiku is more than enough and is ~80% cheaper / faster, which matters a
# lot here — judge_relevance is called once per merged candidate, so it's
# what makes large pools hit Anthropic's per-minute rate limit. Discovery
# stays on Opus because web_search reasoning + structured extraction
# benefits from the bigger model.
JUDGE_MODEL = "claude-haiku-4-5"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}


def max_per_source() -> int:
    """Cap on candidates per source adapter. Lower = smaller rate-limit burst."""
    try:
        return max(1, int(os.environ.get("PROSPECTING_MAX_PER_SOURCE", "5")))
    except ValueError:
        return 5


def _api_key() -> str:
    """Read ANTHROPIC_API_KEY and strip any whitespace/newlines.

    The Railway dashboard (and copy-paste in general) loves to append a
    trailing newline to env-var values. The Anthropic SDK passes the raw
    string through to the `x-api-key` HTTP header, and httpx rejects the
    request with `LocalProtocolError: Illegal header value` before it
    ever hits the wire — surfacing in our logs as a misleading
    "Connection error." Stripping here keeps the SDK happy regardless of
    what the platform did to the value.
    """
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def llm_available() -> bool:
    """True when the Anthropic SDK is installed and a key is in the env."""
    return _SDK_AVAILABLE and bool(_api_key())


_CLIENT: Optional["anthropic.Anthropic"] = None


def _client() -> "anthropic.Anthropic":
    global _CLIENT
    if _CLIENT is None:
        # max_retries=5 lets the SDK absorb transient 429s and 5xx without
        # crashing the whole prospecting run on a single spike.
        _CLIENT = anthropic.Anthropic(api_key=_api_key(), max_retries=5)
    return _CLIENT


# ----------------------------------------------------------------------------
# discover_candidates
# ----------------------------------------------------------------------------

_DISCOVERY_SYSTEM = (
    "You are an AI prospecting agent. Given an ICP (ideal customer profile) "
    "and a target source (github / linkedin / x), use the web_search tool to "
    "find real candidates that publicly match. For each candidate, emit ONE "
    "tool_use call to `emit_candidate`. Be precise: only emit candidates "
    "whose public profile clearly indicates relevance. Never invent names, "
    "URLs, follower counts, or star counts. If a numeric signal isn't "
    "visible from your searches, omit the field rather than guessing. The "
    "`identity` field must be a stable lowercase slug derived from the "
    "person's name (e.g. 'maya-rodriguez') so the same person is mergeable "
    "across sources."
)

_GITHUB_TOOL = {
    "name": "emit_candidate",
    "description": "Emit one GitHub-sourced candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "identity": {"type": "string", "description": "stable lowercase slug, e.g. 'maya-rodriguez'"},
            "name": {"type": "string"},
            "github_handle": {"type": "string"},
            "gh_stars": {"type": "integer", "description": "approx total stars across notable repos"},
            "works_on": {"type": "string", "description": "1-3 word domain tag (e.g. 'observability')"},
            "side": {"type": "string", "enum": ["Builds", "Hires", "Operates"]},
            "evidence_url": {"type": "string", "description": "URL of the profile/repo you saw"},
        },
        "required": ["identity", "name", "gh_stars"],
        "additionalProperties": False,
    },
    "strict": True,
}

_LINKEDIN_TOOL = {
    "name": "emit_candidate",
    "description": "Emit one LinkedIn-sourced candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "identity": {"type": "string"},
            "name": {"type": "string"},
            "role": {"type": "string"},
            "company": {"type": "string"},
            "seniority": {"type": "string", "enum": ["Mid", "Senior", "Staff+", "Leadership"]},
            "linkedin_url": {"type": "string"},
            "offers": {"type": "string", "description": "what this person can offer at the event (short phrase)"},
            "seeks": {"type": "string", "description": "what this person is looking for (short phrase)"},
            "contact_resolved": {"type": "boolean", "description": "true if a profile URL is visible"},
        },
        "required": ["identity", "name"],
        "additionalProperties": False,
    },
    "strict": True,
}

_X_TOOL = {
    "name": "emit_candidate",
    "description": "Emit one X-sourced candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "identity": {"type": "string"},
            "name": {"type": "string"},
            "x_handle": {"type": "string"},
            "x_followers": {"type": "integer"},
            "evidence_url": {"type": "string"},
        },
        "required": ["identity", "name"],
        "additionalProperties": False,
    },
    "strict": True,
}

_SOURCE_TOOL = {"github": _GITHUB_TOOL, "linkedin": _LINKEDIN_TOOL, "x": _X_TOOL}

_SOURCE_GUIDANCE = {
    "github": (
        "Search GitHub for engineers with public OSS work matching the ICP. "
        "Use queries like 'site:github.com <icp domain> <icp tech>' and "
        "'github profile <icp role>'. Look at bios, popular repos, and "
        "starred repos to estimate `gh_stars`. Only emit candidates with "
        "real public footprint."
    ),
    "linkedin": (
        "Search the public web for LinkedIn profiles matching the ICP. Use "
        "queries like 'site:linkedin.com/in <icp role> <icp seniority>' and "
        "'<icp role> at <type-of-company>'. Extract role, company, and "
        "seniority strictly from the SERP snippet or the page contents — "
        "do not fabricate. Set `contact_resolved` to true only when you "
        "actually have a /in/<handle> URL."
    ),
    "x": (
        "Search X (twitter) for accounts with real reach matching the ICP. "
        "Use 'site:x.com <icp domain>' or 'site:twitter.com <icp domain>'. "
        "Extract follower counts only if visible on the page."
    ),
}


def discover_candidates(source: str, icp: dict, max_candidates: int | None = None) -> list[dict]:
    """
    Use Claude + web_search to surface candidates from one source.

    `max_candidates` defaults to PROSPECTING_MAX_PER_SOURCE (env var, 5)
    — a smaller cap keeps the downstream per-candidate judge phase from
    bursting against Anthropic's rate limits on large runs.

    Returns a list of dicts in the per-source shape defined by the emit
    tool's input_schema. The caller (the SourceAdapter) merges these into
    the standard adapter-record shape.
    """
    if max_candidates is None:
        max_candidates = max_per_source()
    tool = _SOURCE_TOOL[source]
    guidance = _SOURCE_GUIDANCE[source]
    user_msg = (
        f"ICP:\n"
        f"  role: {icp.get('role')}\n"
        f"  seniority: {icp.get('seniority')}\n"
        f"  co_stage: {icp.get('co_stage')}\n"
        f"\n"
        f"Source: {source}\n"
        f"\n"
        f"{guidance}\n"
        f"\n"
        f"Emit up to {max_candidates} candidates via the `emit_candidate` "
        f"tool. One call per candidate. Do not write a free-text summary."
    )

    try:
        response = _client().messages.create(
            model=MODEL,
            max_tokens=16000,
            system=[{
                "type": "text",
                "text": _DISCOVERY_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[WEB_SEARCH_TOOL, tool],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001 — surface, but don't crash the run
        # Anthropic SDK's APIConnectionError stringifies to a bare
        # "Connection error." — surface the underlying cause so we can
        # tell DNS / TLS / refused / unreachable apart in logs.
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        print(f"  [llm] discover_candidates({source}) failed: {type(exc).__name__}: {exc}"
              + (f"  (cause: {type(cause).__name__}: {cause})" if cause else ""))
        return []

    out: list[dict] = []
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "emit_candidate":
            out.append(dict(block.input))
    return out


# ----------------------------------------------------------------------------
# judge_relevance
# ----------------------------------------------------------------------------

_RELEVANCE_SYSTEM = (
    "You are a strict ICP gatekeeper. Decide whether the candidate is a real "
    "match for the ICP, not just a plausible one. Bias toward exclusion when "
    "evidence is thin. Use the `emit_verdict` tool to return your decision."
)

_VERDICT_TOOL = {
    "name": "emit_verdict",
    "description": "Emit the relevance verdict for one candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string", "description": "1-2 sentences, plain language"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["relevant", "reason", "confidence"],
        "additionalProperties": False,
    },
    "strict": True,
}


def judge_relevance(candidate: dict, icp: dict) -> tuple[bool, str]:
    """
    Run an LLM gatekeeper over a merged candidate record.

    Returns (relevant, reason). Defaults to (False, "no verdict emitted")
    if the call fails or the model produces no verdict — fail-closed so a
    bad LLM call doesn't push junk into outreach.
    """
    user_msg = (
        "Candidate:\n"
        + json.dumps(candidate, indent=2, default=str)
        + "\n\nICP:\n"
        + json.dumps(icp, indent=2)
    )
    try:
        response = _client().messages.create(
            # Haiku 4.5 — see JUDGE_MODEL note. Binary classifier, doesn't
            # need Opus, and Haiku is the lever that lets large pools not
            # bury the per-minute token budget.
            model=JUDGE_MODEL,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": _RELEVANCE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "emit_verdict"},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        print(f"  [llm] judge_relevance failed: {type(exc).__name__}: {exc}"
              + (f"  (cause: {type(cause).__name__}: {cause})" if cause else ""))
        return False, f"verdict error: {exc}"

    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "emit_verdict":
            return bool(block.input.get("relevant")), str(block.input.get("reason", ""))
    return False, "no verdict emitted"
