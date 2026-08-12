"""backend/observe/harness.py : the common result shape every harness in
backend/observe/harnesses/ returns. A harness is executable code
(a module exposing HARNESS_ID/HARNESS_NAME/HARNESS_VERSION and a run(...) ->
HarnessResult function), never a static UI card -- see each harness module's
own docstring for exactly what it runs and against what data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

_UTC = timezone.utc


def now() -> datetime:
    return datetime.now(_UTC)


@dataclass
class HarnessResult:
    harness_id: str
    name: str
    version: str
    run_timestamp: datetime

    cases_total: int
    cases_passed: int
    cases_failed: int

    metrics: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)   # list[dict]
    provenance: str = "demo"   # "observed" | "synthetic" | "demo" -- see observe/trace.py

    def to_dict(self) -> dict:
        return {
            "harness_id": self.harness_id,
            "name": self.name,
            "version": self.version,
            "run_timestamp": self.run_timestamp.isoformat(),
            "cases_total": self.cases_total,
            "cases_passed": self.cases_passed,
            "cases_failed": self.cases_failed,
            "pass_rate": (round(self.cases_passed / self.cases_total, 4)
                          if self.cases_total else None),
            "metrics": self.metrics,
            "failures": self.failures,
            "provenance": self.provenance,
        }
