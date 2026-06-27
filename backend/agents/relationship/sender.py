"""Backward-compatible re-export. Prefer ``pipeline.send.sender``."""
from .pipeline.send import sender as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
