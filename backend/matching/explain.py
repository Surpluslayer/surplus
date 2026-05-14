"""
explain.py — stubbed in this vendor.

surplus uses its own per-pair rationale generation (lives in
backend/agents/outreach.py:compose() for the connection-note path).
We don't want the library to also bill Anthropic for explain calls.

Keeping the symbol so `run.py`'s top-level import doesn't error. All
entry points either no-op or return empty rationales. Callers should
pass `explain_mode="lazy"` to `run_pipeline` so the upfront-rationale
branch never fires either.
"""
from __future__ import annotations


async def explain_matches(*args, **kwargs):  # noqa: D401
    """No-op stub. Returns an empty dict of rationales."""
    return {}


def explain_pair(*args, **kwargs):
    return {"rationale": "", "intro_message": ""}
