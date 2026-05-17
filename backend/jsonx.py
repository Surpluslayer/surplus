"""
jsonx.py — JSON-from-LLM-output extraction.

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
            return None
    return None
