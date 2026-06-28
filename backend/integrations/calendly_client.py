"""integrations/calendly_client.py : Calendly REST over an OAuth token (pure httpx).
Scheduled events + their invitees -> the normalized meeting shape the shared calendar
ingest consumes ({id, summary, start, attendees[email]}).
"""
from __future__ import annotations

from typing import Optional

import httpx

_API = "https://api.calendly.com"


def _get(token: str, url: str, params: Optional[dict] = None) -> dict:
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                  params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def current_user_uri(token: str) -> str:
    """Calendly keys events by the user's URI -- fetch it from /users/me."""
    return ((_get(token, f"{_API}/users/me").get("resource") or {}).get("uri") or "")


def fetch_scheduled_meetings(token: str, *, user_uri: str, time_min_iso: str,
                             time_max_iso: str, max_results: int = 50) -> list:
    """Active scheduled events in the window, each enriched with its invitee emails,
    normalized for sync_common.ingest_meeting_events."""
    data = _get(token, f"{_API}/scheduled_events", {
        "user": user_uri, "min_start_time": time_min_iso,
        "max_start_time": time_max_iso, "status": "active", "count": max_results})
    out = []
    for e in (data.get("collection") or []):
        uri = e.get("uri") or ""
        emails = []
        try:
            inv = _get(token, f"{uri}/invitees", {"count": 100})
            emails = [(i.get("email") or "").strip().lower()
                      for i in (inv.get("collection") or []) if i.get("email")]
        except Exception:  # noqa: BLE001 : invitee fetch is best-effort
            pass
        out.append({
            "id": uri.rsplit("/", 1)[-1],
            "summary": (e.get("name") or "Meeting").strip(),
            "start": e.get("start_time"),
            "attendees": emails,
        })
    return out
