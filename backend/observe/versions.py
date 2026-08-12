"""backend/observe/versions.py : the version stamp attached to every
DecisionTrace and HarnessResult, so "why did Surplus rank this #1" is always
reconstructible -- inputs + features + these exact version strings + decision
+ outcome. Bump a constant here whenever the corresponding logic changes;
nothing else in this package hardcodes a version string of its own.
"""
from __future__ import annotations

# backend/demo/signal_taxonomy.py's SIGNAL_CATEGORIES + SIGNAL_AFFINITY_SEED shape.
SIGNAL_LIBRARY_VERSION = "signal-taxonomy-v1"

# backend/demo/ranking_trace.py's FACTOR_WEIGHTS + per-factor formulas.
FEATURE_SCHEMA_VERSION = "ranking-trace-v1"

# The ranking function itself (ranking_trace.rank_opportunities / compute_trace).
RANKER_VERSION = "ranker-v1"

# The relationship-specific factor group (strength/recency/trajectory) formulas.
RELATIONSHIP_FEATURE_VERSION = "relationship-features-v1"

# backend/solicitation.py's JURISDICTION_RULES + evaluate() precedence order.
JURISDICTION_POLICY_VERSION = "solicitation-policy-v1"

VERSIONS = {
    "signal_library_version": SIGNAL_LIBRARY_VERSION,
    "feature_schema_version": FEATURE_SCHEMA_VERSION,
    "ranker_version": RANKER_VERSION,
    "relationship_feature_version": RELATIONSHIP_FEATURE_VERSION,
    "jurisdiction_policy_version": JURISDICTION_POLICY_VERSION,
}
