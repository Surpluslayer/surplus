"""Backward-compatible re-export. Prefer ``pipeline.context.gather``."""
from .pipeline.context import gather as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
