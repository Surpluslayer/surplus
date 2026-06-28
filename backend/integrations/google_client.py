"""integrations/google_client.py : thin Gmail + Calendar REST clients over an OAuth
access token (pure httpx, no Google SDK).

Returns NORMALIZED shapes the relationship sync consumes:
  * Gmail messages -> the same mail-item shape `email_sync.counterparts_of` expects
    (from_attendee / to_attendees / date / role), so we reuse that whole pipeline.
  * Calendar events -> flat {id, summary, start, attendees[], ...}.
"""
from __future__ import annotations

import email.utils
from typing import Optional

import httpx

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_GCAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _get(token: str, url: str, params: Optional[dict] = None) -> dict:
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                  params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def _addr(value: Optional[str]) -> dict:
    """One 'Name <email>' header -> {identifier, display_name} (email_sync shape)."""
    name, addr = email.utils.parseaddr(value or "")
    return {"identifier": (addr or "").strip().lower(),
            "display_name": (name or "").strip()}


def _addrs(value: Optional[str]) -> list:
    return [{"identifier": a.strip().lower(), "display_name": (n or "").strip()}
            for n, a in email.utils.getaddresses([value or ""]) if a]


def gmail_fetch_page(token: str, *, own_email: str = "", cursor: Optional[str] = None,
                     newer_than_days: int = 30, max_results: int = 40) -> dict:
    """One page of recent messages, normalized to the email_sync mail-item shape.
    Returns {"items": [...], "cursor": <nextPageToken or None>}."""
    lst = _get(token, f"{_GMAIL}/messages", {
        "q": f"newer_than:{newer_than_days}d", "maxResults": max_results,
        **({"pageToken": cursor} if cursor else {})})
    own = (own_email or "").strip().lower()
    items = []
    for m in (lst.get("messages") or []):
        try:
            full = _get(token, f"{_GMAIL}/messages/{m['id']}",
                        {"format": "metadata",
                         "metadataHeaders": ["From", "To", "Date"]})
        except Exception:  # noqa: BLE001 : one bad message can't sink the page
            continue
        hdrs = {h.get("name", "").lower(): h.get("value", "")
                for h in (full.get("payload") or {}).get("headers") or []}
        frm = _addr(hdrs.get("from"))
        labels = full.get("labelIds") or []
        role = "sent" if ("SENT" in labels or (own and frm["identifier"] == own)) else ""
        items.append({"from_attendee": frm, "to_attendees": _addrs(hdrs.get("to")),
                      "date": hdrs.get("date"), "role": role,
                      "provider_id": m.get("id")})
    return {"items": items, "cursor": lst.get("nextPageToken")}


def fetch_calendar_events(token: str, *, time_min_iso: str, time_max_iso: str,
                          max_results: int = 50) -> list:
    """Flattened calendar events in [time_min, time_max]: {id, summary, start (ISO
    or date), description, attendees[email], html_link}."""
    data = _get(token, _GCAL, {
        "timeMin": time_min_iso, "timeMax": time_max_iso,
        "singleEvents": "true", "orderBy": "startTime", "maxResults": max_results})
    out = []
    for e in (data.get("items") or []):
        st = e.get("start") or {}
        out.append({
            "id": e.get("id"),
            "summary": (e.get("summary") or "").strip(),
            "start": st.get("dateTime") or st.get("date"),
            "description": (e.get("description") or "")[:500],
            "attendees": [(a.get("email") or "").strip().lower()
                          for a in (e.get("attendees") or []) if a.get("email")],
            "html_link": e.get("htmlLink"),
        })
    return out
