"""backend/observe/harnesses/jurisdiction_regression.py : HARNESS #5 --
wraps the existing adversarial fixture matrix
(backend/eval/solicitation_harness.py's build_matrix()) in the common
Harness/HarnessResult shape, with structured per-case failure detail (rule
version, scenario id, expected, actual) instead of solicitation_harness.py's
own pre-formatted failure strings -- the Observe panel needs to render these
fields separately, not parse them back out of a sentence.

Fixtures are UNCHANGED and imported directly from solicitation_harness.py:
this module does not invent or duplicate a single scenario, and inherits
solicitation.py's own caveat -- the jurisdiction VALUES are illustrative
pending legal review; this harness proves the gate LOGIC is sound against the
table as written, it does not certify the table's legal content.
"""
from __future__ import annotations

from ..harness import HarnessResult, now as harness_now
from ..versions import JURISDICTION_POLICY_VERSION
from ...eval.solicitation_harness import build_matrix
from ...solicitation import evaluate

HARNESS_ID = "jurisdiction_regression"
HARNESS_NAME = "Jurisdiction Regression Harness"
HARNESS_VERSION = "v1"


def run() -> HarnessResult:
    scenarios = build_matrix()
    failures = []
    by_category: dict = {}
    passed = 0

    for sc in scenarios:
        cat_bucket = by_category.setdefault(sc.category, {"total": 0, "passed": 0})
        cat_bucket["total"] += 1

        verdict = evaluate(sc.ctx)
        ok = verdict.allowed == sc.expect_allowed
        if ok and sc.expect_reason_contains:
            ok = sc.expect_reason_contains.lower() in verdict.reason.lower()

        if ok:
            passed += 1
            cat_bucket["passed"] += 1
        else:
            failures.append({
                "scenario_id": sc.id,
                "category": sc.category,
                "jurisdiction": sc.ctx.jurisdiction,
                "rule_version": JURISDICTION_POLICY_VERSION,
                "expected": {"allowed": sc.expect_allowed, "reason_contains": sc.expect_reason_contains},
                "actual": {"allowed": verdict.allowed, "reason": verdict.reason},
            })

    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=len(scenarios), cases_passed=passed, cases_failed=len(scenarios) - passed,
        metrics={"by_category": by_category, "rule_version": JURISDICTION_POLICY_VERSION},
        failures=failures, provenance="synthetic",
    )
