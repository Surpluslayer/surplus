"""Backward-compatible re-export. Prefer ``pipeline.compose.drafting``."""
from .pipeline.compose import drafting as _m

globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
