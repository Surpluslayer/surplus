"""Backward-compatible re-export. Prefer ``pipeline.proactive.cadence``."""
from .pipeline.proactive import cadence as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
