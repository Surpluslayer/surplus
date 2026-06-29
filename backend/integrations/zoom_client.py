"""integrations/zoom_client.py : create a Zoom meeting over an OAuth access token.

A BOOKING action (not a read connector). Used when the host wants a Zoom link on a
booked meeting instead of the calendar's native Meet/Teams.
"""
from __future__ import annotations

import httpx

_API = "https://api.zoom.us/v2"


def create_meeting(token: str, *, topic: str, start_iso: str, duration_min: int = 30,
                   timezone: str = "UTC") -> dict:
    """Create a scheduled Zoom meeting. Returns {id, join_url, start_url}."""
    r = httpx.post(
        f"{_API}/users/me/meetings",
        headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
        json={"topic": topic, "type": 2, "start_time": start_iso,
              "duration": max(1, duration_min), "timezone": timezone},
        timeout=20)
    r.raise_for_status()
    d = r.json()
    return {"id": d.get("id"), "join_url": d.get("join_url"),
            "start_url": d.get("start_url")}
