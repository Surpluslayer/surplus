"""backend/observe/trace.py : the universal DecisionTrace contract.

One shape for every observable Surplus decision, regardless of object type.
Feature-specific explanation logic lives in adapters.py (Signal, Relationship,
Opportunity, ...) and produces THIS shape -- the Observe panel renders one
format, not one format per feature (see this package's own docstring: "Use
object-specific adapters feeding a universal trace format").

`provenance` is load-bearing, not decorative: every trace says exactly
whether it comes from a real signed-in account (OBSERVED), the backend/demo
cohort generator (DEMO), or a hand-built known-answer scenario (SYNTHETIC).
The Observe panel must never blend these silently -- see Observe spec
section 11 ("Never silently mix them") and section 15's list of claims not
to make.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import versions as ver

_UTC = timezone.utc

# Closed set -- same discipline as backend/demo/provenance.py's
# PROVENANCE_VALUES (a separate closed set; this one describes a TRACE's
# origin, not a generated DB row's).
OBSERVED = "observed"    # real account data, real production/shared logic
SYNTHETIC = "synthetic"  # constructed known-answer scenario, not real usage
DEMO = "demo"            # backend/demo cohort data (real schema, generated rows)
PROVENANCE_VALUES = frozenset({OBSERVED, SYNTHETIC, DEMO})


def now() -> datetime:
    return datetime.now(_UTC)


@dataclass
class TraceFeature:
    """One inspectable input to a decision. Generalizes
    ranking_trace.RankingFactor's (name, value, evidence) shape with an
    optional weight, so non-ranking decisions (jurisdiction, classification)
    can carry features without a ranking weight attached."""
    name: str
    value: object = None
    evidence: dict = field(default_factory=dict)
    weight: float | None = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "value": self.value, "evidence": self.evidence}
        if self.weight is not None:
            d["weight"] = round(self.weight, 4) if isinstance(self.weight, float) else self.weight
        return d


@dataclass
class DecisionTrace:
    account_id: int
    object_type: str   # "signal" | "signal_library" | "lawyer" | "relationship" |
                        # "opportunity" | "draft" | "jurisdiction" | "outcome"
    object_id: str

    timestamp: datetime

    inputs: dict = field(default_factory=dict)
    entities: dict = field(default_factory=dict)
    features: list = field(default_factory=list)   # list[TraceFeature]
    evidence: dict = field(default_factory=dict)

    decision: str = ""
    score: float | None = None
    rank: int | None = None

    constraints: dict = field(default_factory=dict)
    policy_result: dict | None = None

    versions: dict = field(default_factory=lambda: dict(ver.VERSIONS))

    outcome: dict | None = None
    provenance: str = DEMO
    evaluation_references: list = field(default_factory=list)  # list[dict]

    def __post_init__(self):
        if self.provenance not in PROVENANCE_VALUES:
            raise ValueError(f"unknown trace provenance: {self.provenance!r}")

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "inputs": self.inputs,
            "entities": self.entities,
            "features": [f.to_dict() for f in self.features],
            "evidence": self.evidence,
            "decision": self.decision,
            "score": round(self.score, 4) if isinstance(self.score, float) else self.score,
            "rank": self.rank,
            "constraints": self.constraints,
            "policy_result": self.policy_result,
            "versions": self.versions,
            "outcome": self.outcome,
            "provenance": self.provenance,
            "evaluation_references": self.evaluation_references,
        }
