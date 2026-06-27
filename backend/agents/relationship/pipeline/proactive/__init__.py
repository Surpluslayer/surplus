"""Stage: proactive decision layer — cadence + dated triggers."""
from . import cadence, sweep

# Canonical entry: pipeline.proactive.sweep (run_proactive_sweep, collect_due, …)
proactive = sweep
