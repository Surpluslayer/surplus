"""Backward-compatible re-export. Prefer ``spine.dedup``."""
from .spine import dedup as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
