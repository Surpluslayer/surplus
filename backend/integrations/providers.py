"""integrations/providers.py : the OAuth provider registry.

Adding a source = a config entry here, NOT new flow code. Each provider declares its
OAuth endpoints, the scopes we request, the env vars holding its client credentials,
and (optionally) a userinfo endpoint so we can label the connected account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    auth_url: str
    token_url: str
    scopes: tuple
    client_id_env: str
    client_secret_env: str
    # Extra auth-URL params. Google needs access_type=offline + prompt=consent to
    # return a refresh_token (without them you only get a short-lived access token).
    extra_auth_params: dict = field(default_factory=dict)
    userinfo_url: str = ""        # to fetch the connected account's email/label


GOOGLE = ProviderConfig(
    name="google",
    auth_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes=(
        "openid", "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        # calendar.events grants create/update (booking) AND read of events, so it
        # supersedes the old calendar.readonly -- the read sync keeps working.
        "https://www.googleapis.com/auth/calendar.events",
    ),
    client_id_env="GOOGLE_CLIENT_ID",
    client_secret_env="GOOGLE_CLIENT_SECRET",
    extra_auth_params={"access_type": "offline", "prompt": "consent",
                       "include_granted_scopes": "true"},
    userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
)

MICROSOFT = ProviderConfig(
    name="microsoft",
    auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    scopes=(
        "openid", "email", "offline_access",   # offline_access = the refresh token
        "https://graph.microsoft.com/Mail.Read",
        # Calendars.ReadWrite grants create (booking) and read, so the read sync
        # keeps working off the same scope.
        "https://graph.microsoft.com/Calendars.ReadWrite",
    ),
    client_id_env="MICROSOFT_CLIENT_ID",
    client_secret_env="MICROSOFT_CLIENT_SECRET",
    extra_auth_params={"prompt": "consent"},
    userinfo_url="https://graph.microsoft.com/v1.0/me",
)

CALENDLY = ProviderConfig(
    name="calendly",
    auth_url="https://auth.calendly.com/oauth/authorize",
    token_url="https://auth.calendly.com/oauth/token",
    scopes=(),                      # Calendly OAuth scopes access to the user; no scope strings
    client_id_env="CALENDLY_CLIENT_ID",
    client_secret_env="CALENDLY_CLIENT_SECRET",
    extra_auth_params={},
    userinfo_url="https://api.calendly.com/users/me",   # email nested under .resource
)

PROVIDERS: dict = {GOOGLE.name: GOOGLE, MICROSOFT.name: MICROSOFT,
                   CALENDLY.name: CALENDLY}


def get_provider(name: str) -> Optional[ProviderConfig]:
    return PROVIDERS.get((name or "").strip().lower())
