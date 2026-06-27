"""Backward-compatible re-export. Prefer ``spine.relationships``."""
from .spine import relationships as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
