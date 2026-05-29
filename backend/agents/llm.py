"""
agents/llm.py : Claude-driven prospecting helpers.

Three operations, all gated by ANTHROPIC_API_KEY:

  discover_candidates(source, icp)      web-search-driven discovery per source
  judge_relevance_batch(candidates, icp) LLM gatekeeper : ICP match verdict

`llm_available()` returns True only when the SDK is installed AND a key is
set in the environment. Callers must check it first and fall back to the
mock pool when False (so seed/tests still work offline).

Design notes:
- Model is claude-opus-4-7 (sampling params and budget_tokens are removed
  on Opus 4.7 : adaptive thinking only; the system prompt is the only
  steering knob).
- The system prompt is stable and gets a cache_control breakpoint : every
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


# Discovery runs Sonnet 4.6 : Haiku 4.5 was attempted but doesn't appear
# to support web_search_20260209 (every discover_candidates call started
# 400ing). Sonnet supports the newer tool with dynamic filtering and is
# still the fastest model that works end-to-end. Judge stays on Haiku
# because it's plain text-in / verdict-out, no web_search tool.
MODEL = "claude-sonnet-4-6"
JUDGE_MODEL = "claude-haiku-4-5"
# Cap each adapter's web_search iterations. 1 is enough for the demo :
# a single SERP usually yields 5+ candidates, and a second round adds
# ~15s of latency that wasn't worth it in practice. Raise back to 2 if
# discovery quality drops.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 1}


def max_per_source() -> int:
    """Cap on candidates per source adapter.

    50 default : Exa is generous (one query, ~$0.005, up to 100 results)
    and we have free credits to burn. Judge handles ~50 candidates in
    one batched Haiku call for ~$0.005, so the total cost ceiling per
    /prospect run is ~$0.02 even at this cap.
    """
    try:
        return max(1, int(os.environ.get("PROSPECTING_MAX_PER_SOURCE", "50")))
    except ValueError:
        return 50


def _api_key() -> str:
    """Read ANTHROPIC_API_KEY and strip any whitespace/newlines.

    The Railway dashboard (and copy-paste in general) loves to append a
    trailing newline to env-var values. The Anthropic SDK passes the raw
    string through to the `x-api-key` HTTP header, and httpx rejects the
    request with `LocalProtocolError: Illegal header value` before it
    ever hits the wire : surfacing in our logs as a misleading
    "Connection error." Stripping here keeps the SDK happy regardless of
    what the platform did to the value.
    """
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def llm_available() -> bool:
    """
    True when ANY discovery backend is configured : Exa OR Anthropic.

    Source adapters call this to decide between LLM-driven discovery and
    the mock pool. `discover_candidates()` below picks the actual backend.
    """
    from . import exa
    if exa.exa_available():
        return True
    return _SDK_AVAILABLE and bool(_api_key())


_CLIENT: Optional["anthropic.Anthropic"] = None


def _client() -> "anthropic.Anthropic":
    global _CLIENT
    if _CLIENT is None:
        # max_retries=2: enough to absorb a single 429 / 5xx blip without
        # making the user wait through 5 silent backoff rounds (which can
        # add 30-60s of invisible latency on first-run discovery).
        _CLIENT = anthropic.Anthropic(api_key=_api_key(), max_retries=2)
    return _CLIENT


# ----------------------------------------------------------------------------
# discover_candidates
# ----------------------------------------------------------------------------

_DISCOVERY_SYSTEM = (
    "You are an AI prospecting agent. Given an ICP (ideal customer profile) "
    "and a target source (github / linkedin / x), use the web_search tool to "
    "find real candidates that publicly match. Emit ONE tool_use call to "
    "`emit_candidate` per candidate. Cast a wide net: any real person whose "
    "public signal plausibly aligns with the ICP role + seniority should be "
    "surfaced : a downstream ICP gate decides who stays. Skip only when the "
    "profile clearly contradicts the ICP. Never invent names, URLs, follower "
    "counts, or star counts; if a numeric signal isn't visible, omit the "
    "field. The `identity` field must be a stable lowercase slug derived "
    "from the person's name (e.g. 'maya-rodriguez') so the same person is "
    "mergeable across sources."
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
            "seniority": {"type": "string", "enum": ["Student", "New grad", "Junior", "Senior", "Staff+", "Leadership"]},
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

_SCHOLAR_TOOL = {
    "name": "emit_candidate",
    "description": "Emit one Scholar / research-sourced candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "identity": {"type": "string"},
            "name": {"type": "string"},
            "scholar_url": {"type": "string", "description": "Google Scholar / Semantic Scholar / arXiv profile URL"},
            "scholar_citations": {"type": "integer", "description": "approx total citations across published work"},
            "evidence_url": {"type": "string"},
        },
        "required": ["identity", "name", "scholar_citations"],
        "additionalProperties": False,
    },
    "strict": True,
}

_SOURCE_TOOL = {
    "github": _GITHUB_TOOL,
    "linkedin": _LINKEDIN_TOOL,
    "x": _X_TOOL,
    "scholar": _SCHOLAR_TOOL,
}

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
        "seniority strictly from the SERP snippet or the page contents : "
        "do not fabricate. Set `contact_resolved` to true only when you "
        "actually have a /in/<handle> URL."
    ),
    "x": (
        "Search X (twitter) for accounts with real reach matching the ICP. "
        "Use 'site:x.com <icp domain>' or 'site:twitter.com <icp domain>'. "
        "Extract follower counts only if visible on the page."
    ),
    "scholar": (
        "Search academic sources for researchers whose published work aligns "
        "with the ICP domain. Use queries like 'site:scholar.google.com "
        "<icp domain>', 'site:semanticscholar.org <icp domain>', and "
        "'site:arxiv.org <icp domain>'. Extract approximate total citation "
        "count from the profile or top-paper snippets. Identity slug should "
        "match the same person's slug across sources so the merge can attach "
        "the citation signal to an existing LinkedIn / GitHub record."
    ),
}


def discover_candidates(source: str, icp: dict, max_candidates: int | None = None) -> list[dict]:
    """
    Surface candidates from one source for this ICP.

    Backend selection:
      1. Exa (when EXA_API_KEY is set) : preferred: cheaper, faster,
         structured profile URLs from a search index.
      2. Anthropic Claude + web_search : fallback when EXA is unavailable
         but ANTHROPIC_API_KEY is set.
      3. Caller falls back to the mock pool when neither is available
         (handled by `llm_available()` being False, which the source
         adapters check before calling here).

    Returns a list of dicts in the per-source shape. Same contract
    regardless of which backend produced the result.
    """
    if max_candidates is None:
        max_candidates = max_per_source()

    # Prefer Exa when configured. Only fall through to Claude if Exa
    # returned nothing (e.g., a transient HTTP error).
    from . import exa
    if exa.exa_available():
        out = exa.discover_via_exa(source, icp, max_candidates)
        if out:
            return out
        # Scholar is Exa-only by design : Claude + web_search for
        # researcher pages takes 60-90s and the results don't carry the
        # name-slug we need to merge onto an existing LinkedIn record,
        # so the fallback would just burn 30s of adapter-timeout for
        # zero useful signal. Return empty and let the merge proceed.
        if source == "scholar":
            return []
        # Exa returned empty : fall through to Claude if we have it,
        # otherwise return the empty list.
        if not (_SDK_AVAILABLE and bool(_api_key())):
            return []

    tool = _SOURCE_TOOL[source]
    guidance = _SOURCE_GUIDANCE[source]
    user_msg = (
        f"ICP:\n"
        f"  role: {icp.get('role')}\n"
        f"  seniority: {icp.get('seniority')}\n"
        f"  co_stage: {icp.get('co_stage')}\n"
        f"  city: {icp.get('city') or '(any)'}\n"
        f"\n"
        f"Source: {source}\n"
        f"\n"
        f"{guidance}\n"
        f"\n"
        f"Emit up to {max_candidates} candidates via the `emit_candidate` "
        f"tool. One call per candidate. Do not write a free-text summary."
    )

    try:
        # `output_config` is intentionally NOT set here: the pinned
        # anthropic==0.42.0 raises TypeError on it. Re-add when we bump
        # the SDK to a version that knows the parameter.
        # 110s SDK timeout : slightly under the 120s adapter timeout in
        # prospector.py so the SDK raises a clean APITimeoutError that
        # our except catches, instead of getting cancelled mid-flight.
        response = _client().with_options(timeout=110.0).messages.create(
            model=MODEL,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": _DISCOVERY_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[WEB_SEARCH_TOOL, tool],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001 : surface, but don't crash the run
        # Anthropic SDK's APIConnectionError stringifies to a bare
        # "Connection error." : surface the underlying cause so we can
        # tell DNS / TLS / refused / unreachable apart in logs.
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        print(f"  [llm] discover_candidates({source}) failed: {type(exc).__name__}: {exc}"
              + (f"  (cause: {type(cause).__name__}: {cause})" if cause else ""))
        # Surface to the SPA via the failure-log contextvar so the
        # operator can see whether it was a rate-limit, auth issue, or
        # transport failure (instead of silently getting 0 candidates).
        from . import failure_log
        failure_log.report_failure(
            failure_log.classify_anthropic_exception(exc),
            source=source,
            detail=f"{type(exc).__name__}: {str(exc)[:160]}",
        )
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
    "You are an ICP gatekeeper. Your job is to drop obvious mismatches so "
    "outreach doesn't go to the wrong people. Use the `emit_verdict` (single) "
    "or `emit_verdicts` (batch) tool.\n\n"
    "REJECT (relevant=false) when the candidate looks like ANY of:\n"
    "  - A company / org / agency page where the 'name' is the brand "
    "(e.g. 'Bay Area Event Staffing', 'Acme Solutions', 'Stripe Inc'). "
    "Signal: the name reads like a business, the handle is a brand slug, "
    "or the role is a generic title like 'Manager' / 'Owner' without a "
    "specific function.\n"
    "  - A recruiter, staffing agency, or talent firm (the ICP is the "
    "person being hired, not the recruiter).\n"
    "  - A role in a wholly unrelated function (sales, marketing, HR, ops, "
    "design) when the ICP is engineering / research / technical.\n"
    "  - A career level miles off the target (e.g. intern when target is "
    "Staff+, or VP when target is Junior). One level off is fine.\n"
    "  - No LinkedIn profile or no resolvable contact.\n\n"
    "KEEP (relevant=true) when the candidate is plausibly a real individual "
    "in the right function, even if seniority/stage signal is thin. "
    "Borderline-but-plausible individuals stay; obvious org pages or "
    "wrong-function roles get cut."
)

_BATCH_VERDICT_TOOL = {
    "name": "emit_verdicts",
    "description": "Emit relevance verdicts for the entire candidate batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "description": "One entry per input candidate. Match on `identity`.",
                "items": {
                    "type": "object",
                    "properties": {
                        "identity": {"type": "string"},
                        "relevant": {"type": "boolean"},
                        "reason": {"type": "string", "description": "1-2 sentences"},
                    },
                    "required": ["identity", "relevant", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    },
    "strict": True,
}


def judge_relevance_batch(candidates: list[dict], icp: dict) -> dict[str, tuple[bool, str]]:
    """
    Run the ICP gatekeeper over a whole pool in ONE Haiku call.

    Massive latency win vs calling `judge_relevance` per candidate : for
    a pool of 15 candidates that's 1 API round-trip instead of 15.

    Returns a dict keyed by candidate `identity` → (relevant, reason).
    Missing entries default to (False, "no verdict emitted") so callers
    can treat unjudged candidates as dropped (fail-closed, same as the
    single-call version).
    """
    if not candidates:
        return {}
    user_msg = (
        "ICP:\n"
        + json.dumps(icp, indent=2)
        + "\n\nCandidates (judge each one : emit ONE entry per candidate, "
        "matched by `identity`):\n"
        + json.dumps(candidates, indent=2, default=str)
    )
    out: dict[str, tuple[bool, str]] = {}
    try:
        response = _client().messages.create(
            model=JUDGE_MODEL,
            # 4096 gives Haiku room to emit a verdict for every candidate
            # in the batch without truncating mid-output : truncated
            # verdicts default to "drop" which silently dropped real hits.
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": _RELEVANCE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[_BATCH_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "emit_verdicts"},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        print(f"  [llm] judge_relevance_batch failed: {type(exc).__name__}: {exc}"
              + (f"  (cause: {type(cause).__name__}: {cause})" if cause else ""))
        from . import failure_log
        failure_log.report_failure(
            failure_log.classify_anthropic_exception(exc),
            source="judge",
            detail=f"{type(exc).__name__}: {str(exc)[:160]}",
        )
        # Fail-closed: every candidate gets dropped with the error as reason.
        return {c["identity"]: (False, f"verdict error: {exc}") for c in candidates}

    saw_tool_use = False
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "emit_verdicts":
            saw_tool_use = True
            verdicts = block.input.get("verdicts", [])
            print(f"  [llm.judge] Haiku returned {len(verdicts)} verdicts "
                  f"for {len(candidates)} candidates")
            input_ids = {str(c.get("identity", "")) for c in candidates}
            for v in verdicts:
                ident = v.get("identity")
                if ident:
                    if str(ident) not in input_ids:
                        print(f"  [llm.judge] identity mismatch : "
                              f"Haiku emitted {ident!r}, not in input set")
                    out[str(ident)] = (bool(v.get("relevant")), str(v.get("reason", "")))
            break
    if not saw_tool_use:
        # Haiku ignored the tool and returned text instead — dump the first
        # chunk so we can see what it said.
        text_chunks = [getattr(b, "text", "") for b in response.content
                       if getattr(b, "type", "") == "text"]
        print(f"  [llm.judge] no tool_use block. Text response : "
              f"{(''.join(text_chunks))[:300]!r}")
    return out
