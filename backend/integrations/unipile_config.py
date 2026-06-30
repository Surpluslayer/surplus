"""integrations/unipile_config.py : the ONE place that derives Unipile
credentials from the environment.

Before this, ~a dozen call sites hand-rolled the same two lines:

    dsn = (os.environ.get("UNIPILE_DSN", "") or "").strip().rstrip("/")
    if dsn and not dsn.startswith(("http://", "https://")):
        dsn = f"https://{dsn}"
    api_key = (os.environ.get("UNIPILE_API_KEY", "") or "").strip()

Each copy could drift (forget the https:// prepend, forget the rstrip, etc.).
This module is the single source of truth for that normalization, mirroring the
DSN normalization the UnipileProvider constructor already does.

    unipile_creds() -> (dsn, api_key) when BOTH are set, else None
    unipile_headers(api_key) -> the X-API-KEY auth header dict
"""
from __future__ import annotations

import os
from typing import Optional


def normalize_unipile_dsn(raw: Optional[str]) -> str:
    """Normalize a Unipile DSN: strip whitespace, drop a trailing slash, and
    prepend https:// when the dashboard value omits a scheme (e.g.
    `api40.unipile.com:17054`). Returns '' when unset."""
    dsn = (raw or "").strip().rstrip("/")
    if dsn and not dsn.startswith(("http://", "https://")):
        dsn = f"https://{dsn}"
    return dsn


def unipile_creds() -> Optional[tuple[str, str]]:
    """(dsn, api_key) read from UNIPILE_DSN / UNIPILE_API_KEY and normalized, or
    None when EITHER is unset. The None return is the "Unipile not configured"
    signal callers branch on (raise 503, return [], etc.)."""
    dsn = normalize_unipile_dsn(os.environ.get("UNIPILE_DSN"))
    api_key = (os.environ.get("UNIPILE_API_KEY", "") or "").strip()
    if not (dsn and api_key):
        return None
    return dsn, api_key


def unipile_headers(api_key: str) -> dict[str, str]:
    """The Unipile auth header. Unipile authenticates with X-API-KEY."""
    return {"X-API-KEY": api_key}
