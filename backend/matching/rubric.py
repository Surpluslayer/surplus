"""LLM-synthesized per-event match rubric.

Runs once per event, BEFORE enrichment finishes, on just the raw Person list
plus the user-provided event description. Outputs a JSON rubric that score.py
applies deterministically to every pair.

The rubric is event-aware: hackathon → teammate matching, fellowship → cofounder,
salon → conversation. The same scorer code works across events; only the
weights and gates differ.

Cost: ~$0.03-0.05 per event (one Sonnet call). Cached by event_id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from anthropic import AsyncAnthropic

from backend.matching.schema import Person
from backend.matching.shared import cache as _cache


MODEL = os.environ.get("RUBRIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2500
PROMPT_PATH = Path(__file__).parent / "prompts" / "rubric_synthesis.md"
MATRIX_DIR = Path("data/matches")  # still write a copy here for the matrix output
CACHE_NAMESPACE = "rubric_match"
CACHE_VERSION = "v1"


# ---- Inputs summary ----

def _summarize_people(people: list[Person], top_n: int = 30) -> dict[str, Any]:
    """Produce a compact stats summary the LLM can reason over."""
    if not people:
        return {
            "total_count": 0,
            "ticket_type_counts": {},
            "top_roles": [],
            "top_companies": [],
            "exp_level_counts": {},
        }

    def _norm(s: str) -> str:
        return (s or "").strip()

    tickets = Counter(_norm(p.ticket_type) or "unknown" for p in people)
    exp = Counter(_norm(p.exp_level) or "unknown" for p in people)

    # Role keywords — tokenize titles, count occurrences of meaningful tokens
    role_tokens: Counter = Counter()
    for p in people:
        text = f"{p.role} {p.title}".lower()
        for tok in re.findall(r"[a-z][a-z\-]{2,}", text):
            if tok in _STOPWORDS:
                continue
            role_tokens[tok] += 1

    company_counter = Counter(_norm(p.company) for p in people if _norm(p.company) and _norm(p.company).lower() not in {"n/a", "na", "stealth", ""})

    return {
        "total_count": len(people),
        "ticket_type_counts": dict(tickets.most_common()),
        "top_roles": [t for t, _ in role_tokens.most_common(top_n)],
        "top_companies": [c for c, _ in company_counter.most_common(top_n)],
        "exp_level_counts": dict(exp.most_common()),
    }


_STOPWORDS = {
    "the", "and", "of", "at", "for", "with", "from", "to", "in", "on",
    "engineer", "intern", "ai", "ml",  # too common to be distinguishing
}


# ---- Prompt ----

def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text()


def _build_prompt(event_name: str, event_description: str, summary: dict[str, Any]) -> str:
    """Use plain replace() not .format() because the prompt has literal JSON braces."""
    substitutions = {
        "{event_name}": event_name or "(not provided)",
        "{event_description}": event_description or "(not provided)",
        "{total_count}": str(summary.get("total_count", 0)),
        "{ticket_type_counts}": json.dumps(summary.get("ticket_type_counts", {})),
        "{top_roles}": json.dumps(summary.get("top_roles", [])),
        "{top_companies}": json.dumps(summary.get("top_companies", [])),
        "{exp_level_counts}": json.dumps(summary.get("exp_level_counts", {})),
    }
    out = _load_prompt_template()
    for k, v in substitutions.items():
        out = out.replace(k, v)
    return out


# ---- Cache ----

def _event_id(event_name: str, event_description: str) -> str:
    key = "|".join([(event_name or "").strip().lower(), (event_description or "").strip().lower()])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _read_cache(event_id: str) -> Optional[dict[str, Any]]:
    return _cache.get(CACHE_NAMESPACE, CACHE_VERSION, MODEL, event_id)


def _write_cache(event_id: str, data: dict[str, Any]) -> None:
    _cache.put(CACHE_NAMESPACE, data, CACHE_VERSION, MODEL, event_id)
    # Also write a human-readable copy under data/matches/<event_id>/rubric.json
    # so it's easy to inspect alongside the matrix output.
    p = MATRIX_DIR / event_id / "rubric.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


# ---- LLM call ----

def _extract_json(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---- Fallback rubric ----

def _fallback_rubric(summary: dict[str, Any]) -> dict[str, Any]:
    """Sensible defaults when the LLM call fails. Generic, not event-specific."""
    tickets = summary.get("ticket_type_counts", {})
    # Symmetric matrix: every observed pair = 0.7, identical type = 0.9
    role_matrix: dict[str, float] = {}
    types = list(tickets.keys()) or ["unknown"]
    for a in types:
        for b in types:
            key = f"{a}|{b}"
            role_matrix[key] = 0.9 if a == b else 0.7
    return {
        "event_type": "other",
        "event_type_reasoning": "fallback default; LLM rubric synthesis unavailable.",
        "match_intent": "mixed",
        "match_intent_reasoning": "fallback default.",
        "role_pair_matrix": role_matrix,
        "hard_gates": {
            "min_similar_score": 0.05,
            "min_role_pair_score": 0.30,
            "require_same_city": False,
        },
        "weights": {
            "axis_blend": {"similar": 0.30, "complementary": 0.70},
            "similar": {
                "domain_overlap": 0.40,
                "conviction_overlap": 0.30,
                "background_resonance": 0.20,
                "city_match": 0.10,
            },
            "complementary": {
                "skill_complement": 0.40,
                "experience_asymmetry": 0.25,
                "role_complement": 0.20,
                "domain_expansion": 0.15,
            },
        },
        "anti_signals": {
            "direct_competitor_multiplier": 0.25,
            "profile_clone_multiplier": 0.70,
            "seniority_gap_3_or_more_multiplier": 0.65,
            "explicit_mismatch_multiplier": 0.40,
        },
        "notes_for_humans": "Generic fallback rubric. The matching engine is balanced across all observed roles.",
        "_fallback": True,
    }


def _validate_rubric(r: dict[str, Any]) -> bool:
    required = {"event_type", "match_intent", "role_pair_matrix", "weights"}
    if not required.issubset(r.keys()):
        return False
    weights = r.get("weights", {})
    if not isinstance(weights, dict):
        return False
    if not isinstance(weights.get("axis_blend"), dict):
        return False
    return True


# ---- Public API ----

async def synthesize_rubric(
    event_name: str,
    event_description: str,
    people: list[Person],
    *,
    use_cache: bool = True,
    anthropic_client: Optional[AsyncAnthropic] = None,
) -> dict[str, Any]:
    """Return a rubric dict for this event. Caches by (event_name, description) hash.

    Never raises — falls back to a sensible default rubric on any failure so
    the pipeline can always proceed.
    """
    event_id = _event_id(event_name, event_description)
    if use_cache:
        cached = _read_cache(event_id)
        if cached is not None:
            return cached

    summary = _summarize_people(people)
    prompt = _build_prompt(event_name, event_description, summary)

    client = anthropic_client or AsyncAnthropic()
    telemetry: dict[str, Any] = {"model": MODEL}
    try:
        t0 = time.time()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        telemetry["elapsed_s"] = round(time.time() - t0, 2)
        telemetry["input_tokens"] = getattr(resp.usage, "input_tokens", 0)
        telemetry["output_tokens"] = getattr(resp.usage, "output_tokens", 0)
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        parsed = _extract_json(text)
    except Exception as e:
        print(f"[rubric] LLM error: {e!r} — using fallback")
        rubric = _fallback_rubric(summary)
        rubric["_telemetry"] = {**telemetry, "error": repr(e)}
        rubric["_event_id"] = event_id
        rubric["_generated_at"] = datetime.now(timezone.utc).isoformat()
        if use_cache:
            _write_cache(event_id, rubric)
        return rubric

    if not parsed or not _validate_rubric(parsed):
        print("[rubric] invalid LLM output — using fallback")
        rubric = _fallback_rubric(summary)
        rubric["_telemetry"] = {**telemetry, "parse_failed": True}
    else:
        rubric = parsed
        rubric["_telemetry"] = telemetry

    # Safety clamp on min_similar_score — the geometric/weighted-sum blend
    # already ranks shared-context pairs higher, so the gate is mainly there
    # to drop pairs with truly zero signal. Cap at 0.03 so it doesn't
    # over-filter cross-role matches where complementarity is the point.
    gates = rubric.setdefault("hard_gates", {})
    if gates.get("min_similar_score", 0) > 0.03:
        gates["min_similar_score"] = 0.0

    rubric["_event_id"] = event_id
    rubric["_generated_at"] = datetime.now(timezone.utc).isoformat()
    rubric["_event_name"] = event_name
    rubric["_summary_used"] = summary
    if use_cache:
        _write_cache(event_id, rubric)
    return rubric
