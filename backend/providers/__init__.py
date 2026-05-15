"""
providers/ — pluggable LinkedIn outreach providers.

Currently only Unipile is implemented. Adding another provider is a one-file
change: drop a new module that implements LinkedInProvider, register it in
get_provider() below, and set PROVIDER=<name> in env.
"""
from __future__ import annotations
import os
from functools import lru_cache

from .base import (
    LinkedInProvider,
    LeadPayload,
    ProviderResult,
    CanonicalEvent,
    CANONICAL_STATES,
)
from .unipile import UnipileProvider


@lru_cache(maxsize=1)
def get_provider() -> LinkedInProvider:
    """Resolve the configured provider. Defaults to Unipile in DRY_RUN."""
    name = os.environ.get("PROVIDER", "unipile").lower().strip()
    if name == "unipile":
        return UnipileProvider.from_env()
    raise ValueError(f"Unknown PROVIDER={name!r} (expected 'unipile')")


def reset_provider_cache() -> None:
    """Test hook — clears the cached provider so env-var changes apply."""
    get_provider.cache_clear()


__all__ = [
    "LinkedInProvider",
    "LeadPayload",
    "ProviderResult",
    "CanonicalEvent",
    "CANONICAL_STATES",
    "UnipileProvider",
    "get_provider",
    "reset_provider_cache",
]
