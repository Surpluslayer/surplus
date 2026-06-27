"""Backward-compatible re-export. Prefer ``pipeline.proactive.sweep``."""
from .pipeline.proactive import cadence, sweep as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
