"""Backward-compatible re-export. Prefer ``pipeline.context.reconcile``."""
from .pipeline.context import reconcile as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
