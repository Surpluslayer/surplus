"""On-demand LLM explanation for a matched pair.

The pair scoring pipeline (matcher_lib) is structured-output: it produces
component scores (skill_complement, role_complement, domain_expansion, ...).
Those numbers tell you *how strongly* the model judged this pair, but not
why a human should walk over and shake the other person's hand.

This module turns the cached EnrichedPerson + the cached pair components
into one short paragraph, on demand — so we only pay for the LLM call
when a user clicks "Why?".
"""
from __future__ import annotations

import os
from typing import Any, Optional

from anthropic import AsyncAnthropic


_MODEL = os.environ.get("PAIR_EXPLAIN_MODEL", "claude-haiku-4-5-20251001")


def _profile_lines(p) -> str:
    """Compact rendering of an EnrichedPerson for the prompt."""
    parts = [f"Name: {p.name}"]
    if p.title or p.role:
        parts.append(f"Role: {p.title or p.role}")
    if p.company:
        parts.append(f"Company: {p.company}")
    if p.roles_history:
        prior = "; ".join(
            f"{r.get('title','?')} at {r.get('company','?')}"
            for r in p.roles_history[:4]
        )
        parts.append(f"Career: {prior}")
    if p.domains:
        parts.append(f"Domains: {', '.join(p.domains[:6])}")
    if p.tech_stack:
        parts.append(f"Tech: {', '.join(p.tech_stack[:8])}")
    if p.conviction_themes:
        parts.append(f"Conviction themes: {', '.join(p.conviction_themes[:5])}")
    if p.previous_experiences:
        parts.append(f"Shipped: {'; '.join(p.previous_experiences[:3])}")
    if p.bio_text:
        bio = p.bio_text.strip().replace("\n", " ")
        if len(bio) > 280:
            bio = bio[:277] + "…"
        parts.append(f"Bio: {bio}")
    return "\n".join(parts)


def _components_summary(pair: dict[str, Any]) -> str:
    """Render the top component scores so the model can ground its answer."""
    comp = pair.get("components") or {}
    rows: list[tuple[str, float]] = []
    for axis_name in ("similar", "complementary"):
        for k, v in (comp.get(axis_name) or {}).items():
            rows.append((f"{axis_name}.{k}", float(v)))
    rows.sort(key=lambda x: -x[1])
    top = rows[:5]
    if not top:
        return "(no component breakdown available)"
    return "\n".join(f"  {k}: {v:.2f}" for k, v in top)


async def explain_pair(person_a, person_b, pair: Optional[dict[str, Any]] = None) -> str:
    """Generate one short paragraph on why this pair is worth connecting.

    Grounded on enriched profile data + the structured component scores.
    Two-to-three sentences, concrete, second-person. No fluff.
    """
    if person_a is None or person_b is None:
        return "Couldn't find enrichment data for one of these people."

    prompt = f"""You're an event organizer briefing a guest on why we seated them near a specific other guest. Be concrete, factual, and short — two to three sentences. Cite the specific overlap or asymmetry that creates value. No generic platitudes.

PERSON A:
{_profile_lines(person_a)}

PERSON B:
{_profile_lines(person_b)}

The structured matcher already scored this pair on several axes (0-1). The strongest signals:
{_components_summary(pair or {})}

Write the explanation as if telling Person A: "Worth meeting B because…". 2-3 sentences max. Do not enumerate the scores; use them to ground your reasoning, but speak in plain language."""

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=_MODEL,
            max_tokens=240,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return text or "(no explanation produced)"
    except Exception as exc:  # noqa: BLE001
        return f"Explanation failed: {type(exc).__name__}: {exc}"
