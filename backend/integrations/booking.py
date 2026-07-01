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

import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from .. import models
from . import oauth

# Providers that can HOST an event (have a writable calendar). Calendly/Granola can't.
_CAL_PROVIDERS = ("google", "microsoft")

# Default scheduling policy for the AUTONOMOUS booker (agent_book_meeting). The
# host has no timezone column yet, so the agent books in a single shared business
# zone (env-overridable) rather than guessing. Business hours are local to that tz.
_DEFAULT_TZ = (os.environ.get("SURPLUS_BOOKING_TZ") or "America/Los_Angeles").strip()
_BUSINESS_START_HOUR = int(os.environ.get("SURPLUS_BOOKING_START_HOUR", "9"))   # 9am
_BUSINESS_END_HOUR = int(os.environ.get("SURPLUS_BOOKING_END_HOUR", "17"))      # 5pm
_LOOKAHEAD_BUSINESS_DAYS = 5     # search the next ~week of weekdays for a slot
_SLOT_STEP_MIN = 30              # granularity we scan candidate start times at
# Don't book same-instant; give the contact lead time before the first candidate.
_MIN_LEAD_HOURS = int(os.environ.get("SURPLUS_BOOKING_MIN_LEAD_HOURS", "2"))


def _tzinfo(tz_name: str):
    """ZoneInfo for `tz_name`, falling back to UTC if the zone is unknown (so a
    bad env value can never crash the booker)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a calendar event 'start' (ISO datetime, possibly trailing 'Z', or a
    bare 'YYYY-MM-DD' all-day date) into an aware UTC datetime. None when unusable."""
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:                                   # bare all-day date -> midnight UTC
            dt = datetime.fromisoformat(s + "T00:00:00+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _busy_blocks(db, acct, *, time_min: datetime, time_max: datetime) -> list:
    """The host's existing events in [time_min, time_max] as (start_utc, end_utc)
    tuples, read from the SAME provider client the read-sync uses. Best-effort: on
    any upstream hiccup return [] (so the booker degrades to 'treat as free' rather
    than failing). Used to never double-book."""
    token = oauth.get_valid_access_token(db, acct)
    if not token:
        return []
    if acct.provider == "google":
        from .google_client import fetch_calendar_events
    else:
        from .outlook_client import fetch_calendar_events
    try:
        events = fetch_calendar_events(
            token, time_min_iso=time_min.isoformat(),
            time_max_iso=time_max.isoformat(), max_results=250)
    except Exception:  # noqa: BLE001
        return []
    blocks = []
    for e in events or []:
        st = _parse_dt(e.get("start"))
        if st is None:
            continue
        # fetch_calendar_events doesn't carry an end; assume a default-length block
        # so we leave a buffer around each existing event rather than abutting it.
        en = st + timedelta(minutes=max(_SLOT_STEP_MIN, 30))
        blocks.append((st, en))
    return blocks


def _overlaps(start: datetime, end: datetime, blocks: list) -> bool:
    return any(start < b_end and b_start < end for b_start, b_end in blocks)


def find_open_slot(db, acct, *, duration_min: int = 30, tz: str = _DEFAULT_TZ,
                   lookahead_days: int = _LOOKAHEAD_BUSINESS_DAYS) -> Optional[str]:
    """Earliest reasonable open slot on the host's calendar over the next
    `lookahead_days` BUSINESS days, returned as an ISO 8601 string WITH the tz
    offset (ready to pass straight to book_meeting). Business-hours + timezone
    aware; respects existing events (never double-books). None if nothing fits.

    Deterministic and side-effect-free apart from the read, so it's unit-testable
    by stubbing fetch_calendar_events.
    """
    zi = _tzinfo(tz)
    now_local = datetime.now(zi)
    earliest = now_local + timedelta(hours=max(0, _MIN_LEAD_HOURS))
    # Window for the busy read: from now to the end of the lookahead window.
    horizon_local = (now_local + timedelta(days=lookahead_days + 2)).replace(
        hour=_BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
    blocks = _busy_blocks(db, acct,
                          time_min=now_local.astimezone(timezone.utc),
                          time_max=horizon_local.astimezone(timezone.utc))

    business_days_seen = 0
    day = now_local.date()
    while business_days_seen < lookahead_days:
        wd = day.weekday()
        if wd >= 5:                              # Sat/Sun: skip, don't count
            day = day + timedelta(days=1)
            continue
        business_days_seen += 1
        # Walk candidate starts on this day at _SLOT_STEP_MIN granularity, keeping
        # the whole meeting inside business hours.
        slot_local = datetime.combine(day, time(_BUSINESS_START_HOUR, 0), tzinfo=zi)
        last_start = datetime.combine(
            day, time(_BUSINESS_END_HOUR, 0), tzinfo=zi) - timedelta(minutes=duration_min)
        while slot_local <= last_start:
            if slot_local >= earliest:
                start_utc = slot_local.astimezone(timezone.utc)
                end_utc = start_utc + timedelta(minutes=duration_min)
                if not _overlaps(start_utc, end_utc, blocks):
                    return slot_local.isoformat()
            slot_local += timedelta(minutes=_SLOT_STEP_MIN)
        day = day + timedelta(days=1)
    return None


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


def host_timezone(db, user) -> str:
    """The host's REAL timezone (IANA), read from their connected calendar so the
    agent proposes + books in the user's local time, not a shared default. Uses the
    Google primary-calendar timezone; falls back to _DEFAULT_TZ for Outlook (whose
    API returns Windows tz names) or when nothing is readable."""
    try:
        acct = _pick_account(db, user.id, None)
        if acct is None or acct.provider != "google":
            return _DEFAULT_TZ
        token = oauth.get_valid_access_token(db, acct)
        if not token:
            return _DEFAULT_TZ
        from .google_client import get_calendar_timezone
        return get_calendar_timezone(token) or _DEFAULT_TZ
    except Exception:  # noqa: BLE001
        return _DEFAULT_TZ


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


# ── Autonomous booking building blocks (used by the draft+send pipeline) ───────
# These power the "a meeting is the next step" path: the drafter puts a scheduling
# link or a proposed time into the message text, and SEND fires the actual booking.
# Booking is therefore a SIDE EFFECT OF SENDING a meeting-proposal draft, gated by
# the same automation flag the auto-send path uses (manual: on host send; auto: on
# auto-send). Nothing here creates an event on its own.

# source_type stamped on the RelationshipInteraction we write when a meeting is
# booked, so the spine timeline shows it and we can dedup against an open loop.
_BOOKED_SOURCE_TYPE = "meeting_booked"


def contact_email(db, contact) -> str:
    """Best email to INVITE this contact at: the Contact.email column first, then
    a strong ContactIdentity of kind 'email'. Lowercased; '' when none on file (the
    caller must then stage the link/text WITHOUT creating a broken invite)."""
    direct = (getattr(contact, "email", None) or "").strip().lower()
    if direct:
        return direct
    try:
        row = (db.query(models.ContactIdentity)
               .filter_by(user_id=contact.user_id, contact_id=contact.id, kind="email")
               .order_by(models.ContactIdentity.is_primary.desc(),
                         models.ContactIdentity.confidence.desc())
               .first())
    except Exception:  # noqa: BLE001
        row = None
    return (getattr(row, "value", "") or "").strip().lower()


def calendly_scheduling_link(db, user) -> Optional[str]:
    """The host's public Calendly link, if Calendly is connected + refreshable.
    Used so a meeting-proposal draft can offer self-serve scheduling (the link IS
    the booking, so no event is fired on send). None when unavailable."""
    acct = (db.query(models.ConnectedAccount)
            .filter_by(user_id=user.id, provider="calendly", status="active").first())
    if acct is None:
        return None
    token = oauth.get_valid_access_token(db, acct)
    if not token:
        return None
    try:
        from .calendly_client import scheduling_url
        return scheduling_url(token) or None
    except Exception:  # noqa: BLE001
        return None


def _zoom_connected(db, user) -> bool:
    return (db.query(models.ConnectedAccount)
            .filter_by(user_id=user.id, provider="zoom", status="active")
            .first()) is not None


def _existing_booking(db, user, contact) -> Optional[dict]:
    """The most recent meeting_booked interaction for this (host, contact) whose
    meeting is still in the FUTURE, as its stored meta dict. Idempotency guard: a
    second send for the same open loop returns this instead of double-booking. None
    when there's no live booking."""
    import json
    try:
        rows = (db.query(models.RelationshipInteraction)
                .filter_by(actor_user_id=user.id, contact_id=contact.id,
                           source_type=_BOOKED_SOURCE_TYPE)
                .order_by(models.RelationshipInteraction.occurred_at.desc())
                .all())
    except Exception:  # noqa: BLE001
        return None
    now = datetime.now(timezone.utc)
    for ri in rows:
        try:
            meta = json.loads(ri.meta_json or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        start = _parse_dt(meta.get("start_iso") or meta.get("start"))
        if start is not None and start > now:
            return {**meta, "interaction_id": ri.id, "already_booked": True}
    return None


def propose_meeting_slot(db, user, *, duration_min: int = 30) -> dict:
    """Decide HOW to offer a meeting in a draft, WITHOUT creating anything.

    Returns {mode, ...} for the drafter to weave into the message text and to
    carry as the staged draft's booking payload:
      * {"mode": "calendly", "scheduling_url": ...}    Calendly connected: the
        link is the booking, so SEND fires nothing.
      * {"mode": "propose_time", "start_iso", "tz", "duration_min", "with_zoom"}
        a concrete open slot the host can confirm; SEND creates the event+invite.
      * {"mode": "none", "reason": ...}                 no calendar + no Calendly:
        the draft falls back to plain text asking for the contact's availability.

    Pure-ish (one calendar read); deterministic given the calendar state, so the
    send step can re-validate the same payload before firing."""
    link = calendly_scheduling_link(db, user)
    if link:
        return {"mode": "calendly", "scheduling_url": link,
                "duration_min": duration_min}
    acct = _pick_account(db, user.id, None)
    if acct is None:
        return {"mode": "none", "reason": "no calendar or calendly connected"}
    tz = host_timezone(db, user)
    slot = find_open_slot(db, acct, duration_min=duration_min, tz=tz)
    if not slot:
        return {"mode": "none",
                "reason": "no open slot in the next business week"}
    return {"mode": "propose_time", "start_iso": slot, "tz": tz,
            "duration_min": duration_min, "with_zoom": _zoom_connected(db, user)}


def agent_book_meeting(db, user, contact, *, topic: str, start_iso: str = "",
                       duration_min: int = 30, tz: str = _DEFAULT_TZ,
                       with_zoom: Optional[bool] = None) -> dict:
    """Create the calendar event + invite the CONTACT, and record it. The action
    behind a SENT meeting-proposal draft (manual: host send; auto: auto-send).

    Picks an open slot when `start_iso` is empty, resolves the contact's email to
    invite them, attaches a Zoom link when Zoom is connected (else the native
    Meet/Teams link), and writes a `meeting_booked` RelationshipInteraction.

    IDEMPOTENT: if a future meeting is already booked for this (host, contact), it
    returns that booking with already_booked=True instead of creating a second
    event. EMAIL REQUIRED: with no invitable email it raises ValueError so the
    caller stages the text/link WITHOUT a broken, attendee-less event.

    Returns {provider, id, html_link, video_url, start, attendees, already_booked}.
    Raises ValueError (clear reason) on no-email / no-calendar / bad-time / upstream.
    """
    import json

    prior = _existing_booking(db, user, contact)
    if prior is not None:
        return prior

    email = contact_email(db, contact)
    if not email:
        raise ValueError("contact has no email on file to invite")

    if with_zoom is None:
        with_zoom = _zoom_connected(db, user)

    # Default tz -> the host's REAL calendar timezone. An explicit tz (e.g. from a
    # proposal payload) is already host-resolved upstream, so honor it as-is.
    if tz == _DEFAULT_TZ:
        tz = host_timezone(db, user)

    acct = _pick_account(db, user.id, None)
    if acct is None:
        raise ValueError("no calendar connected")

    start = (start_iso or "").strip()
    if not start:
        start = find_open_slot(db, acct, duration_min=duration_min, tz=tz) or ""
    if not start:
        raise ValueError("no open slot in the next business week")

    ev = book_meeting(
        db, user, attendee_email=email, attendee_name=(contact.name or ""),
        title=topic, start_iso=start, duration_min=duration_min, tz=tz,
        add_video=True, notify=True, with_zoom=bool(with_zoom))

    # Record the booked meeting on the spine so it shows on the timeline and the
    # idempotency guard can see it. Link the contact's most recent prospect for
    # the per-event tie when one exists (best-effort).
    prospect_id = None
    try:
        prospects = sorted(getattr(contact, "prospects", []) or [],
                           key=lambda p: getattr(p, "id", 0))
        prospect_id = prospects[-1].id if prospects else None
    except Exception:  # noqa: BLE001
        prospect_id = None
    meta = {"start_iso": ev.get("start") or start, "duration_min": duration_min,
            "tz": tz, "video_url": ev.get("video_url"),
            "html_link": ev.get("html_link"), "event_id": ev.get("id"),
            "provider": ev.get("provider"), "attendee_email": email,
            "with_zoom": bool(with_zoom)}
    try:
        ri = models.RelationshipInteraction(
            actor_user_id=user.id, prospect_id=prospect_id, contact_id=contact.id,
            source_type=_BOOKED_SOURCE_TYPE, interaction_type="meeting",
            direction="outbound", occurred_at=datetime.now(timezone.utc),
            title=(topic or "Meeting").strip()[:200],
            summary=f"Booked a meeting with {contact.name or email} for "
                    f"{ev.get('start') or start}.",
            meta_json=json.dumps(meta), visibility="private")
        db.add(ri)
        db.commit()
    except Exception:  # noqa: BLE001 : a logging miss must not unwind a real invite
        db.rollback()

    return {**ev, "already_booked": False, "start_iso": ev.get("start") or start}
