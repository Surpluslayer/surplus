"""integrations/outlook_client.py : Microsoft Graph mail + calendar clients over an
OAuth access token (pure httpx). Normalized to the SAME shapes the spine sync
consumes -- Graph messages match email_sync.counterparts_of, events match
sync_common.ingest_meeting_events -- so Outlook reuses the Google paths' machinery.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

_GRAPH = "https://graph.microsoft.com/v1.0/me"


def _get(token: str, url: str, params: Optional[dict] = None) -> dict:
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                  params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def _post(token: str, url: str, body: dict) -> dict:
    r = httpx.post(url, headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                   json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def _attendee(ea: Optional[dict]) -> dict:
    """Graph emailAddress object -> {identifier, display_name} (email_sync shape)."""
    e = (ea or {}).get("emailAddress") or {}
    return {"identifier": (e.get("address") or "").strip().lower(),
            "display_name": (e.get("name") or "").strip()}


def outlook_fetch_page(token: str, *, own_email: str = "", cursor: Optional[str] = None,
                       newer_than_days: int = 30, max_results: int = 40) -> dict:
    """One page of recent messages, normalized to the email_sync mail-item shape.
    Graph paginates via @odata.nextLink (a full URL), which we pass back as cursor."""
    own = (own_email or "").strip().lower()
    if cursor:
        data = _get(token, cursor)                 # nextLink already carries params
    else:
        since = (datetime.now(timezone.utc)
                 - timedelta(days=newer_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = _get(token, f"{_GRAPH}/messages", {
            "$top": max_results,
            "$select": "from,toRecipients,sentDateTime",
            "$filter": f"sentDateTime ge {since}",
            "$orderby": "sentDateTime desc"})
    items = []
    for m in (data.get("value") or []):
        frm = _attendee(m.get("from"))
        role = "sent" if (own and frm["identifier"] == own) else ""
        items.append({
            "from_attendee": frm,
            "to_attendees": [_attendee(t) for t in (m.get("toRecipients") or [])],
            "date": m.get("sentDateTime"), "role": role,
            "provider_id": m.get("id"),
        })
    return {"items": items, "cursor": data.get("@odata.nextLink")}


def fetch_calendar_events(token: str, *, time_min_iso: str, time_max_iso: str,
                          max_results: int = 50) -> list:
    """Flattened calendar events in [min,max]: {id, summary, start, attendees[email]}."""
    data = _get(token, f"{_GRAPH}/calendarView", {
        "startDateTime": time_min_iso, "endDateTime": time_max_iso,
        "$top": max_results, "$orderby": "start/dateTime",
        "$select": "id,subject,start,attendees,bodyPreview"})
    out = []
    for e in (data.get("value") or []):
        out.append({
            "id": e.get("id"),
            "summary": (e.get("subject") or "").strip(),
            "start": (e.get("start") or {}).get("dateTime"),
            "description": (e.get("bodyPreview") or "")[:500],
            "attendees": [a["emailAddress"]["address"].strip().lower()
                          for a in (e.get("attendees") or [])
                          if (a.get("emailAddress") or {}).get("address")],
        })
    return out


def create_calendar_event(token: str, *, summary: str, start_iso: str, end_iso: str,
                          attendees: list, description: str = "", tz: str = "UTC",
                          add_video: bool = True, notify: bool = True) -> dict:
    """Create an event on the host's calendar (Graph POST /me/events). Graph sends the
    invite to attendees automatically on create, so `notify=False` omits attendees and
    just holds the slot (Graph has no per-create sendUpdates toggle). add_video=True
    attaches a Teams link. Returns {id, html_link, video_url, start, attendees}."""
    body: dict = {
        "subject": summary,
        "start": {"dateTime": start_iso, "timeZone": tz},
        "end": {"dateTime": end_iso, "timeZone": tz},
    }
    if notify:
        body["attendees"] = [{"emailAddress": {"address": a}, "type": "required"}
                             for a in attendees if a]
    if description:
        body["body"] = {"contentType": "text", "content": description}
    if add_video:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    data = _post(token, f"{_GRAPH}/events", body)
    return {
        "id": data.get("id"),
        "html_link": data.get("webLink"),
        "video_url": (data.get("onlineMeeting") or {}).get("joinUrl"),
        "start": (data.get("start") or {}).get("dateTime"),
        "attendees": [(a.get("emailAddress") or {}).get("address")
                      for a in (data.get("attendees") or [])],
    }
