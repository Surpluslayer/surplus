"""integrations/booking.py : create a calendar meeting with a contact (Phase-2 WRITE).

The FIRST action connector (vs the read connectors): given a contact + a time, create
an event on the host's connected calendar (Google or Outlook), invite the contact, and
attach a native video link (Google Meet / Teams) -- so no separate Zoom integration is
needed for "book a meeting with a link".

Outward-facing (the contact gets a calendar invite), so this is an EXPLICIT host action
via POST /api/integrations/calendar/book -- there is no agent tool that calls it, and
any future AUTO-booking must go behind the automation gate. Reuses the same
ConnectedAccount + oauth.get_valid_access_token machinery as the read connectors.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .. import models
from . import oauth

# Providers that can HOST an event (have a writable calendar). Calendly/Granola can't.
_CAL_PROVIDERS = ("google", "microsoft")


def _end_iso(start_iso: str, duration_min: int) -> str:
    """start + duration, preserving the start's offset. Raises ValueError on a non-ISO
    start (the caller maps it to a 400). Normalizes a trailing 'Z' (UTC) to '+00:00'
    since datetime.fromisoformat rejects 'Z' before Python 3.11 and clients send it."""
    s = (start_iso or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    start = datetime.fromisoformat(s)
    return (start + timedelta(minutes=max(1, duration_min))).isoformat()


def _pick_account(db, user_id: int, provider: Optional[str]):
    """The host's calendar-capable connected account. Honors an explicit `provider`,
    else prefers Google then Microsoft. None when nothing usable is connected."""
    q = db.query(models.ConnectedAccount).filter_by(user_id=user_id, status="active")
    if provider:
        return q.filter_by(provider=provider).first()
    rows = {r.provider: r for r in
            q.filter(models.ConnectedAccount.provider.in_(_CAL_PROVIDERS)).all()}
    return rows.get("google") or rows.get("microsoft")


def _zoom_link(db, user, *, topic: str, start_iso: str, duration_min: int, tz: str) -> Optional[str]:
    """Create a Zoom meeting if the host has Zoom connected; returns the join URL or None.
    Best-effort -- a Zoom hiccup falls back to the calendar's native video link."""
    acct = (db.query(models.ConnectedAccount)
            .filter_by(user_id=user.id, provider="zoom", status="active").first())
    if acct is None:
        return None
    token = oauth.get_valid_access_token(db, acct)
    if not token:
        return None
    try:
        from .zoom_client import create_meeting
        return create_meeting(token, topic=topic, start_iso=start_iso,
                              duration_min=duration_min, timezone=tz).get("join_url")
    except Exception:  # noqa: BLE001
        return None


def book_meeting(db, user, *, attendee_email: str, attendee_name: str = "",
                 title: str, start_iso: str, duration_min: int = 30, tz: str = "UTC",
                 description: str = "", add_video: bool = True, notify: bool = True,
                 with_zoom: bool = False, provider: Optional[str] = None) -> dict:
    """Create a calendar event inviting `attendee_email`, returning
    {provider, id, html_link, video_url, start, attendees}.

    Raises ValueError with a clear reason the route maps to a 4xx:
      * "no calendar connected"          -> 409 (connect Google/Outlook first)
      * "<provider> needs reconnection"  -> 409 (token can't refresh)
      * "invalid start time ..."         -> 400
      * "<provider> calendar error: ..." -> 400 (upstream API rejected it)
    """
    acct = _pick_account(db, user.id, provider)
    if acct is None:
        raise ValueError("no calendar connected")
    token = oauth.get_valid_access_token(db, acct)
    if not token:
        raise ValueError(f"{acct.provider} needs reconnection")
    try:
        end_iso = _end_iso(start_iso, duration_min)
    except (TypeError, ValueError):
        raise ValueError("invalid start time (expected ISO 8601, e.g. 2026-07-01T15:00:00-07:00)")

    # Zoom (optional): if requested + connected, make a Zoom meeting and put its link on
    # the event instead of the native Meet/Teams. Falls back to native video if Zoom isn't
    # available, so booking never fails just because Zoom is.
    zoom_url = None
    use_native_video = add_video
    if with_zoom:
        zoom_url = _zoom_link(db, user, topic=title, start_iso=start_iso,
                              duration_min=duration_min, tz=tz)
        if zoom_url:
            use_native_video = False
            description = (description + f"\nZoom: {zoom_url}").strip()

    if acct.provider == "google":
        from .google_client import create_calendar_event
    else:
        from .outlook_client import create_calendar_event
    try:
        ev = create_calendar_event(
            token, summary=title, start_iso=start_iso, end_iso=end_iso,
            attendees=[attendee_email] if attendee_email else [],
            description=description, tz=tz, add_video=use_native_video, notify=notify)
    except Exception as exc:  # noqa: BLE001 : surface a clean reason, never a stack
        raise ValueError(f"{acct.provider} calendar error: {type(exc).__name__}")
    if zoom_url:
        ev["video_url"] = zoom_url
    return {"provider": acct.provider, **ev}
