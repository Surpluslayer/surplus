"""
hosts.py : helpers for the in-person host (event.surpluslayer.com).

The in-person surface is served on a dedicated host but shares this backend
(API + auth) with the desktop product on the apex. A few places need to know
"did this request come from the in-person host?" : the auth redirect target
(keep the LinkedIn flow on event.) and the send gate (connect + send are free
on event., paywalled on the apex). Centralized here so the host set is defined
once.
"""
from __future__ import annotations
import os
from urllib.parse import urlsplit


# Hosts that are under our control AND map to the in-person surface. Used to
# guard against honoring a forged Origin (open-redirect / paywall-bypass).
_FIRST_PARTY_SUFFIX = "surpluslayer.com"


def inperson_hosts() -> set[str]:
    """The configured in-person host set (mirrors backend/main.py's SPA
    routing). Env-overridable; defaults to event.surpluslayer.com."""
    return {
        h.strip().lower()
        for h in (os.environ.get("INPERSON_HOSTS") or "event.surpluslayer.com").split(",")
        if h.strip()
    }


def is_inperson_host(host: str) -> bool:
    """True when `host` is the in-person surface. Matches the configured set or
    the `event.` subdomain convention (so preview subdomains Just Work)."""
    h = (host or "").split(":")[0].lower()
    if not h:
        return False
    return h in inperson_hosts() or h.startswith("event.")


def is_first_party(host: str) -> bool:
    """True when host is one of ours (*.surpluslayer.com). Gate redirects /
    paywall relaxations on this so a forged Origin header can't exploit them."""
    h = (host or "").split(":")[0].lower()
    return bool(h) and (h == _FIRST_PARTY_SUFFIX or h.endswith("." + _FIRST_PARTY_SUFFIX))


def request_browser_host(request) -> str:
    """The user-facing host the SPA fetch came from.

    Behind the CDN / Railway the Host header is rewritten to the origin's
    internal name, but the Origin header (the SPA fetch is same-origin) and
    X-Forwarded-Host preserve the real user-facing host. Prefer Origin, then
    X-Forwarded-Host, then Host."""
    origin = request.headers.get("origin") or ""
    if origin:
        host = urlsplit(origin).hostname or ""
        if host:
            return host.lower()
    xfh = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if xfh:
        return xfh.split(":")[0].lower()
    return (request.headers.get("host") or "").split(":")[0].lower()
