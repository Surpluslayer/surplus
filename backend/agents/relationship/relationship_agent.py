"""Backward-compatible re-export. Prefer ``pipeline.agent.run``."""
from .pipeline.agent import run as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
