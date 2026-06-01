"""
jsonx.py : JSON-from-LLM-output extraction.

Models love to wrap JSON in markdown fences or leak prose around it.
`extract_json` is the recovery strategy: strip ```json fences, try a
plain parse, fall back to the largest balanced-brace substring.

Used by every Claude-call site that expects a JSON-shaped response.
"""
from __future__ import annotations
import json
import re
from typing import Any, Optional


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Return the first JSON object found in `text`, or None.

    Robust to: leading/trailing prose, ```json fences, and stray
    tokens after a valid object. Prefill the assistant turn with "{"
    upstream so the model is forced into JSON-mode, then prepend "{"
    back onto its response before calling this.
    """
    text = (text or "").strip()
    if not text:
        return None
    fence = _FENCE_RE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    # Last resort: the object was TRUNCATED (e.g. the model hit max_tokens
    # mid-value, leaving no closing brace). Salvage the largest valid prefix by
    # cutting back to the last completed top-level member and closing the object.
    if start != -1:
        recovered = _recover_truncated_object(text[start:])
        if recovered is not None:
            return recovered
    return None


def _recover_truncated_object(s: str) -> Optional[dict[str, Any]]:
    """Best-effort parse of a JSON object whose tail was cut off.

    Walks the string tracking string-literal state and brace/bracket depth,
    remembering each index where a top-level member completes (a comma at
    depth 1). On failure to parse the whole thing, we cut at the last such
    boundary and append the missing closing braces. This salvages the early,
    high-value fields (e.g. `dimensions`) and only drops the incomplete trailing
    field (typically verbose `notes`). Returns None if nothing parses.
    """
    in_str = False
    esc = False
    depth = 0
    member_ends: list[int] = []   # indices just AFTER a completed depth-1 member
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 1:
                member_ends.append(i + 1)   # a nested obj/array member just closed
        elif ch == "," and depth == 1:
            member_ends.append(i)           # scalar/string member completed
    # Try cutting at each boundary from the latest backward, closing braces.
    for cut in reversed(member_ends):
        candidate = s[:cut].rstrip().rstrip(",") + "}"
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None
