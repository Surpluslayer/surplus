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
    """Resolve the configured provider using env-var account_id.

    This is the single-tenant fallback path — used by tests, by webhook
    handlers that don't yet know which user owns the prospect, and as a
    safety net during the multi-tenant migration. Production user-initiated
    sends should use get_provider_for_user(user) instead.
    """
    name = os.environ.get("PROVIDER", "unipile").lower().strip()
    if name == "unipile":
        return UnipileProvider.from_env()
    raise ValueError(f"Unknown PROVIDER={name!r} (expected 'unipile')")


def get_provider_for_user(user) -> LinkedInProvider:
    """Resolve a provider configured to send on behalf of `user`.

    Same Unipile DSN + API key as the env config (those are operator-level
    secrets, shared across tenants), but the account_id comes from the
    user's row — i.e. the LinkedIn account THEY connected through the
    Sign-in-with-LinkedIn flow.

    Caller responsibility: only call with a user that has a non-empty
    unipile_account_id and linkedin_status == "active". The auth gate
    on the route should already enforce signed-in, but stale connections
    (LinkedIn forced re-login) need to be re-checked here.
    """
    if not getattr(user, "unipile_account_id", None):
        raise ValueError(f"User {getattr(user, 'id', '?')} has no LinkedIn connection")
    name = os.environ.get("PROVIDER", "unipile").lower().strip()
    if name != "unipile":
        raise ValueError(f"Unknown PROVIDER={name!r} (expected 'unipile')")
    # Build a fresh per-user instance (intentionally NOT cached — different
    # users get different provider objects). Pulls DSN/API key from env.
    from ..providers.unipile import _env_bool
    return UnipileProvider(
        dsn=os.environ.get("UNIPILE_DSN"),
        api_key=os.environ.get("UNIPILE_API_KEY"),
        account_id=user.unipile_account_id,
        webhook_secret=os.environ.get("UNIPILE_WEBHOOK_SECRET"),
        dry_run=_env_bool("UNIPILE_DRY_RUN", True),
        require_signature=_env_bool("UNIPILE_REQUIRE_SIGNATURE", True),
    )


def get_provider_for_prospect(prospect, fallback: LinkedInProvider) -> LinkedInProvider:
    """Route a send through the owning user's LinkedIn account.

    Webhooks are server-to-server (no session cookie), so the only way to
    find the right account is to walk the data: Prospect → Event → User →
    that user's Unipile account_id. If the chain is broken or the user's
    LinkedIn connection is stale, fall back to the env-var operator account.
    """
    event = getattr(prospect, "event", None)
    if event is not None and getattr(event, "user_id", None):
        owner = getattr(event, "user", None)
        if owner is not None and getattr(owner, "unipile_account_id", None):
            try:
                return get_provider_for_user(owner)
            except Exception:
                pass
    return fallback


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
    "get_provider_for_user",
    "get_provider_for_prospect",
    "reset_provider_cache",
]
