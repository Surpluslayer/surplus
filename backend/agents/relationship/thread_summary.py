"""Backward-compatible re-export. Prefer ``pipeline.context.summary``."""
from .pipeline.context import summary as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
