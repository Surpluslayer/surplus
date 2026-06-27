"""Backward-compatible re-export. Prefer ``pipeline.send.flow``."""
from .pipeline.send import flow as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
