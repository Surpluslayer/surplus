"""eval/drafting_quality_harness.py : the moat metric for the drafting
pipeline -- how much better is the real 4-stage composer than the floor a
fast-follower could stand up in a sprint?

This is the operationalized version of "prove ranking/drafting quality is
meaningfully better than what a competitor could stand up quickly." It runs
the SAME scenario set already used for prompt regression testing
(messaging_eval._CASES -- reused, not duplicated, so this harness measures
against the same ground truth the team already trusts) through two
composers:

  * NAIVE BASELINE  -- a plain template, zero voice grounding, zero
    relationship-stage awareness, zero open-loop tracking, zero
    confidence-gated fact selection, no LLM call at all. This is deliberately
    the floor, not a strawman: it's what "signal detection plus an LLM
    drafting a warm email" looks like with a weekend of engineering. Because
    it's a pure template, it's zero-variance and always runnable -- no API
    key, no network.

  * PRODUCTION       -- the real pipeline (pipeline.compose.drafting.
    compose_from_context). Needs ANTHROPIC_API_KEY. This is the seam: when
    credentials are available, this arm runs for real and the gap between
    the two becomes a measured number instead of a claim.

Track the GAP over time, not either score in isolation -- a shrinking gap
means the floor is catching up (bad); a widening gap, especially one that
widens because of the edit-feedback loop (see the harness docstring's
companion note below), is the actual defensible signal.

Run it:
    python -m backend.eval.drafting_quality_harness
    python -m backend.eval.drafting_quality_harness --dump gap_report.json

NEXT STEP (not yet wired -- needs real usage data, not a coding task): feed
every lawyer's draft-then-edit pair back in as a new scenario, the same way
`solicitation_harness.py`'s categories should grow from real near-misses.
That loop is what makes this number improve on its own instead of only when
someone remembers to add a scenario.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..agents.relationship.messaging_eval import _CASES, _gates, _word_count
from ..agents import voice as voice_mod

Composer = Callable[[dict, str, str], dict]  # (ctx, reason, channel) -> {"body": str}


def naive_baseline_compose(ctx: dict, reason: str, channel: str) -> dict:
    """The floor: what a fast-follower assembles without any of the
    voice/relationship/confidence-gating machinery. No LLM call -- pure
    template -- so this arm is always runnable and always identical for the
    same input, useful as a stable comparison anchor."""
    name = ctx.get("name") or "there"
    facts = ctx.get("facts") or {}
    update = facts.get("latest_update")
    next_step = facts.get("next_step")
    if update:
        body = f"Hi {name}, hope this finds you well. Congrats on the update -- {update}! Just checking in and wanted to reconnect."
    elif next_step:
        body = f"Hi {name}, hope you're doing well. Wanted to touch base and follow up on {next_step}."
    else:
        body = f"Hi {name}, hope this finds you well. Just checking in -- would love to catch up soon."
    return {"body": body}


class ProductionComposerUnavailable(RuntimeError):
    """Raised when the real pipeline can't run -- surfaced loudly in the
    report rather than silently skipped, so the missing credential is
    visible as the blocker it actually is."""


def production_compose(ctx: dict, reason: str, channel: str) -> dict:
    """Wraps the real 4-stage composer. This is the one arm of the moat
    comparison that needs ANTHROPIC_API_KEY -- everything else in this
    harness runs with zero external dependencies."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise ProductionComposerUnavailable(
            "ANTHROPIC_API_KEY not set -- the production arm of the gap "
            "comparison needs real credentials. The naive-baseline arm and "
            "the harness plumbing are real and already running without it."
        )
    from ..agents.relationship.pipeline.compose.drafting import compose_from_context
    return compose_from_context(ctx, reason, channel)


def _ctx_from_case(case: dict) -> dict:
    """Same ctx shape messaging_eval._eval_case builds -- kept in sync
    manually since duplicating a few fields is safer than importing a
    private helper that assumes messaging_eval's own module-level cache."""
    return {
        "name": case["name"], "role": case["role"], "company": case["company"],
        "prior": case.get("prior") or [],
        "register": voice_mod.detect_register(
            [m.get("text") or "" for m in (case.get("prior") or [])
             if m.get("who") == "them"]),
        "facts": case.get("facts") or {},
        "voice_block": "",  # baseline/gap comparison: judged on structure, not voice replay
    }


@dataclass
class ArmResult:
    id: str
    draft: str = ""
    gates: dict = field(default_factory=dict)
    blocked_reason: Optional[str] = None


def _run_arm(composer: Composer, case: dict) -> ArmResult:
    ctx = _ctx_from_case(case)
    try:
        out = composer(ctx, case.get("reason") or "following up", "linkedin")
        draft = (out or {}).get("body") or ""
        return ArmResult(id=case["id"], draft=draft, gates=_gates(draft))
    except ProductionComposerUnavailable as e:
        return ArmResult(id=case["id"], blocked_reason=str(e))
    except Exception as e:  # noqa: BLE001 - report the failure, don't crash the harness
        return ArmResult(id=case["id"], blocked_reason=f"{type(e).__name__}: {e}")


_GATE_KEYS = ("no_em_dash", "concise", "not_generic")


def run_gap_eval(verbose: bool = True, dump: Optional[str] = None) -> dict:
    per_case = []
    for case in _CASES:
        baseline = _run_arm(naive_baseline_compose, case)
        production = _run_arm(production_compose, case)
        per_case.append({"id": case["id"], "baseline": baseline, "production": production})

    baseline_pass = sum(
        1 for r in per_case if r["baseline"].gates and all(r["baseline"].gates.get(k) for k in _GATE_KEYS)
    )
    production_ran = [r for r in per_case if r["production"].blocked_reason is None]
    production_pass = sum(
        1 for r in production_ran if all(r["production"].gates.get(k) for k in _GATE_KEYS)
    )

    report = {
        "cases": len(per_case),
        "baseline_gate_pass": f"{baseline_pass}/{len(per_case)}",
        "production_gate_pass": (
            f"{production_pass}/{len(production_ran)}" if production_ran
            else "BLOCKED (0 cases ran -- see per_case[*].production.blocked_reason)"
        ),
        "production_blocked": len(production_ran) == 0,
        "per_case": [
            {
                "id": r["id"],
                "baseline_draft": r["baseline"].draft,
                "baseline_gates": r["baseline"].gates,
                "production_draft": r["production"].draft,
                "production_gates": r["production"].gates,
                "production_blocked_reason": r["production"].blocked_reason,
            }
            for r in per_case
        ],
    }
    if dump:
        with open(dump, "w") as f:
            json.dump(report, f, indent=2)
    if verbose:
        _print_report(report)
    return report


def _print_report(report: dict) -> None:
    print("\n=== DRAFTING QUALITY GAP (naive baseline vs. production) ===")
    print(f"{'case':22} {'baseline gates':16} production")
    for c in report["per_case"]:
        b = c["baseline_gates"]
        b_str = "/".join("Y" if b.get(k) else "n" for k in _GATE_KEYS) if b else "?"
        if c["production_blocked_reason"]:
            p_str = "BLOCKED"
        else:
            p = c["production_gates"]
            p_str = "/".join("Y" if p.get(k) else "n" for k in _GATE_KEYS)
        print(f"{c['id']:22} {b_str:16} {p_str}")
    print(f"\nbaseline gate pass rate:   {report['baseline_gate_pass']}")
    print(f"production gate pass rate: {report['production_gate_pass']}")
    if report["production_blocked"]:
        print(
            "\nProduction arm did not run on any case -- set ANTHROPIC_API_KEY to "
            "close the loop and turn this into a real measured gap instead of a "
            "structural placeholder. The gates/scenario matrix are real and "
            "already validated against the baseline arm above."
        )
    print()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=str, default=None, help="write results JSON")
    args = ap.parse_args()
    run_gap_eval(dump=args.dump)
