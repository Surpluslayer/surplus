"""Backward-compatible re-export. Prefer ``spine.memory``."""
from .spine import memory as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
