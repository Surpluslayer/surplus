"""Cross-cutting diagnostics for the relationship layer."""
from ..pipeline.proactive import sweep as proactive
from .status import (
    _fact_stats,
    _send_outcomes,
    relationship_status,
)
