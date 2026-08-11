"""eval/solicitation_harness.py : the adversarial regression suite for the
Rule 7.3 gate (backend/solicitation.py).

This is NOT the unit test file (tests/test_solicitation.py) -- that pins a
handful of hand-picked cases confirming the gate's own logic does what it was
written to do. This is the thing described as the actual moat: a
systematically-generated, adversarial scenario matrix with HAND-LABELED
expected outcomes, scored by CATEGORY (never blended into one aggregate
number), run repeatedly as the jurisdiction table and the gate logic evolve.

The categories exist because a single "pass rate" hides exactly the
distinction that matters: 98% overall with the one failing category being
"unknown jurisdiction fail-closed" is a materially different risk profile
than 98% with the failures spread evenly across boundary cases.

Run it:
    python -m backend.eval.solicitation_harness
    python -m backend.eval.solicitation_harness --dump report.json

STATUS: the scenario matrix's ground truth is derived from
JURISDICTION_RULES' own values (legitimate -- these are known inputs, not a
re-derivation via evaluate() itself) plus the RelationshipType exemption
invariant. It inherits solicitation.py's own caveat: the jurisdiction VALUES
are illustrative pending legal review. This harness proves the GATE LOGIC is
sound against the table as written; it does not certify the table's content.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..solicitation import (
    JURISDICTION_RULES,
    RelationshipType,
    SolicitationContext,
    Verdict,
    evaluate,
)

_KNOWN_JURISDICTIONS = tuple(JURISDICTION_RULES.keys())
_UNKNOWN_JURISDICTIONS = ("ZZ", "XX", "us", "usa", "")  # includes malformed input


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    ctx: SolicitationContext
    expect_allowed: bool
    # Optional: a substring that must appear in verdict.reason, for scenarios
    # where "allowed/blocked" alone doesn't prove the RIGHT reason fired.
    expect_reason_contains: Optional[str] = None


def _build_matrix() -> list[Scenario]:
    scenarios: list[Scenario] = []
    today = date(2026, 8, 11)

    # ── category: exemption_bypass ──────────────────────────────────────────
    # Invariant: EVERY exempt relationship type must be allowed, even paired
    # with parameters that would otherwise block a prospect outright. This is
    # a legitimate ground-truth derivation (not circular): "exempt overrides
    # everything" is the rule itself, independent of any one jurisdiction's
    # table values.
    exempt_types = [
        RelationshipType.EXISTING_CLIENT, RelationshipType.FORMER_CLIENT,
        RelationshipType.FAMILY_OR_CLOSE_PERSONAL, RelationshipType.LAWYER,
        RelationshipType.SOPHISTICATED_BUSINESS_USER,
    ]
    for rt in exempt_types:
        scenarios.append(Scenario(
            id=f"exempt_{rt.value}_over_realtime_and_cap",
            category="exemption_bypass",
            ctx=SolicitationContext(
                jurisdiction="FL", channel="phone_call", relationship_type=rt,
                matter_is_sensitive=True, triggering_event_date=today,
                today=today, recent_solicitation_count_in_window=99,
            ),
            expect_allowed=True,
            expect_reason_contains="exempt",
        ))

    # ── category: realtime_channel_block ────────────────────────────────────
    for jur in _KNOWN_JURISDICTIONS:
        for channel in ("phone_call", "live_chat"):
            scenarios.append(Scenario(
                id=f"realtime_block_{jur}_{channel}",
                category="realtime_channel_block",
                ctx=SolicitationContext(
                    jurisdiction=jur, channel=channel,
                    relationship_type=RelationshipType.PROSPECT,
                ),
                expect_allowed=False,
                expect_reason_contains="real-time",
            ))

    # ── category: written_channel_disclosure ────────────────────────────────
    for jur, rule in JURISDICTION_RULES.items():
        for channel in ("email", "linkedin_dm"):
            scenarios.append(Scenario(
                id=f"written_disclosure_{jur}_{channel}",
                category="written_channel_disclosure",
                ctx=SolicitationContext(
                    jurisdiction=jur, channel=channel,
                    relationship_type=RelationshipType.PROSPECT,
                ),
                expect_allowed=True,
            ))

    # ── category: cooldown_boundary ─────────────────────────────────────────
    # Off-by-one is the classic bug here: "within N days" boundaries at
    # exactly N-1, N, and N+1 elapsed days.
    fl_cooldown = JURISDICTION_RULES["FL"].sensitive_matter_cooldown_days  # 30
    for delta, expect_allowed in ((fl_cooldown - 1, False), (fl_cooldown, True),
                                   (fl_cooldown + 1, True)):
        scenarios.append(Scenario(
            id=f"cooldown_boundary_FL_elapsed_{delta}d",
            category="cooldown_boundary",
            ctx=SolicitationContext(
                jurisdiction="FL", channel="email",
                relationship_type=RelationshipType.PROSPECT,
                matter_is_sensitive=True,
                triggering_event_date=today - timedelta(days=delta),
                today=today,
            ),
            expect_allowed=expect_allowed,
        ))
    scenarios.append(Scenario(
        id="cooldown_sensitive_no_triggering_date",
        category="cooldown_boundary",
        ctx=SolicitationContext(
            jurisdiction="FL", channel="email",
            relationship_type=RelationshipType.PROSPECT,
            matter_is_sensitive=True, triggering_event_date=None, today=today,
        ),
        expect_allowed=False,  # fail closed: sensitive + unknown timing = blocked
        expect_reason_contains="no triggering-event date",
    ))

    # ── category: volume_cap_boundary ───────────────────────────────────────
    fl_cap, _ = JURISDICTION_RULES["FL"].max_solicitations_per_window  # (1, 30)
    for count, expect_allowed in ((fl_cap - 1, True), (fl_cap, False), (fl_cap + 1, False)):
        scenarios.append(Scenario(
            id=f"volume_cap_boundary_FL_count_{count}",
            category="volume_cap_boundary",
            ctx=SolicitationContext(
                jurisdiction="FL", channel="email",
                relationship_type=RelationshipType.PROSPECT,
                recent_solicitation_count_in_window=count,
            ),
            expect_allowed=expect_allowed,
        ))
    # NY/CA have no volume cap configured -- verify that's actually respected
    # (a bug here would silently cap jurisdictions that shouldn't be capped).
    for jur in ("NY", "CA"):
        scenarios.append(Scenario(
            id=f"no_volume_cap_configured_{jur}",
            category="volume_cap_boundary",
            ctx=SolicitationContext(
                jurisdiction=jur, channel="email",
                relationship_type=RelationshipType.PROSPECT,
                recent_solicitation_count_in_window=10_000,
            ),
            expect_allowed=True,
        ))

    # ── category: unknown_jurisdiction_failclosed ───────────────────────────
    for jur in _UNKNOWN_JURISDICTIONS:
        scenarios.append(Scenario(
            id=f"unknown_jurisdiction_realtime_{jur or 'empty'}",
            category="unknown_jurisdiction_failclosed",
            ctx=SolicitationContext(
                jurisdiction=jur, channel="phone_call",
                relationship_type=RelationshipType.PROSPECT,
            ),
            expect_allowed=False,
        ))
        scenarios.append(Scenario(
            id=f"unknown_jurisdiction_written_still_disclosed_{jur or 'empty'}",
            category="unknown_jurisdiction_failclosed",
            ctx=SolicitationContext(
                jurisdiction=jur, channel="email",
                relationship_type=RelationshipType.PROSPECT,
            ),
            expect_allowed=True,  # written is permitted even fail-closed...
        ))

    # ── category: jurisdiction_case_normalization ───────────────────────────
    scenarios.append(Scenario(
        id="lowercase_jurisdiction_code_normalized",
        category="jurisdiction_case_normalization",
        ctx=SolicitationContext(
            jurisdiction="fl", channel="phone_call",
            relationship_type=RelationshipType.PROSPECT,
        ),
        expect_allowed=False,  # should resolve to FL's rule, not fail-closed-by-accident
        expect_reason_contains="FL",
    ))

    return scenarios


@dataclass
class CategoryResult:
    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)


def run_harness(verbose: bool = True, dump: Optional[str] = None) -> dict:
    scenarios = _build_matrix()
    by_category: dict[str, CategoryResult] = {}

    for sc in scenarios:
        cat = by_category.setdefault(sc.category, CategoryResult())
        cat.total += 1
        verdict: Verdict = evaluate(sc.ctx)
        ok = verdict.allowed == sc.expect_allowed
        if ok and sc.expect_reason_contains:
            ok = sc.expect_reason_contains.lower() in verdict.reason.lower()
        if ok:
            cat.passed += 1
        else:
            cat.failures.append(
                f"{sc.id}: expected allowed={sc.expect_allowed}"
                + (f" reason~'{sc.expect_reason_contains}'" if sc.expect_reason_contains else "")
                + f", got allowed={verdict.allowed} reason='{verdict.reason}'"
            )

    report = {
        "total_scenarios": len(scenarios),
        "total_passed": sum(c.passed for c in by_category.values()),
        "categories": {
            cat: {"total": r.total, "passed": r.passed, "failures": r.failures}
            for cat, r in sorted(by_category.items())
        },
    }
    if dump:
        with open(dump, "w") as f:
            json.dump(report, f, indent=2)
    if verbose:
        _print_report(report)
    return report


def _print_report(report: dict) -> None:
    print("\n=== SOLICITATION HARNESS (adversarial, by category) ===")
    print(f"{'category':38} {'pass/total':12} status")
    any_fail = False
    for cat, r in report["categories"].items():
        status = "OK" if r["passed"] == r["total"] else "FAIL"
        if status == "FAIL":
            any_fail = True
        print(f"{cat:38} {r['passed']}/{r['total']:<9} {status}")
        for f in r["failures"]:
            print(f"    - {f}")
    total, passed = report["total_scenarios"], report["total_passed"]
    print(f"\nTOTAL: {passed}/{total} ({100 * passed / total:.1f}%)")
    print("REGRESSION" if any_fail else "clean" + "\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=str, default=None, help="write results JSON")
    args = ap.parse_args()
    run_harness(dump=args.dump)
