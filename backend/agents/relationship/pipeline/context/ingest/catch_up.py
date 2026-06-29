"""LinkedIn Catch Up ingest → ContactFact store.

Catch Up (birthdays, job changes, work anniversaries, education) lives behind the
host's LinkedIn session. LinkedIn SSR-embeds card rows in the Catch Up HTML pages
(`/mynetwork/catch-up/<kind>/`) — no DevTools capture required for the default path.

Ingest path:
  1. `run_catch_up_ingest` fetches the Catch Up page via Unipile `linkedin_raw`.
  2. `parse_catch_up_html` extracts listitem cards (name, slug, event detail).
  3. Matched contacts get `upsert_fact` rows; proactive sweep fires triggers.

Optional: env `LINKEDIN_CATCHUP_RAW_REQUEST` for a captured SDUI/XHR JSON endpoint.
Interim signal: profile `birthdate` from Unipile `fetch_profile` — `store_profile_birthdate`.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

CATCH_UP_KINDS = ("birthday", "job_change", "work_anniversary", "education", "all")

# UI paths (for probe script / docs — data loads via SDUI, not these HTML URLs).
CATCH_UP_PATHS = {
    "birthday": "/mynetwork/catch-up/birthday/",
    "job_change": "/mynetwork/catch-up/job_changes",
    "work_anniversary": "/mynetwork/catch-up/work_anniversaries",
    "education": "/mynetwork/catch-up/education",
    "all": "/mynetwork/catch-up/all/",
}

_FACT_BY_KIND = {
    "birthday": "birthday",
    "work_anniversary": "work_anniversary",
    "job_change": "life_event",
    "education": "life_event",
    "all": "life_event",
}

_RECURRING_KINDS = frozenset({"birthday", "work_anniversary"})

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class CatchUpEvent:
    kind: str
    name: str
    linkedin_public_id: str = ""
    linkedin_url: str = ""
    detail: str = ""
    month: Optional[int] = None
    day: Optional[int] = None


def unwrap_linkedin_raw(resp: Any) -> Any:
    """Normalize Unipile linkedin_raw envelope to parsed JSON or raw string."""
    if resp is None:
        return None
    if not isinstance(resp, dict):
        return resp
    data = resp.get("data")
    if data is None:
        data = resp.get("output")
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return data
        return data
    return data if data is not None else resp


def _norm_slug(value: str) -> str:
    return (value or "").strip().lower().rstrip("/").split("/")[-1]


def _merge_name(first: str, last: str) -> str:
    return " ".join(x for x in ((first or "").strip(), (last or "").strip()) if x).strip()


def _walk(node: Any, out: list[CatchUpEvent], kind: str) -> None:
    if isinstance(node, dict):
        pub = (node.get("publicIdentifier")
               or node.get("public_identifier")
               or node.get("vanityName")
               or "")
        if not pub and isinstance(node.get("miniProfile"), dict):
            mp = node["miniProfile"]
            pub = mp.get("publicIdentifier") or mp.get("public_identifier") or ""
        first = node.get("firstName") or node.get("first_name") or ""
        last = node.get("lastName") or node.get("last_name") or ""
        if isinstance(node.get("miniProfile"), dict):
            mp = node["miniProfile"]
            first = first or mp.get("firstName") or ""
            last = last or mp.get("lastName") or ""
            pub = pub or mp.get("publicIdentifier") or ""
        name = _merge_name(first, last) or (node.get("name") or "").strip()
        bday = node.get("birthdate") or node.get("birthday") or node.get("birthDate")
        month = day = None
        if isinstance(bday, dict):
            try:
                month = int(bday.get("month")) if bday.get("month") is not None else None
                day = int(bday.get("day")) if bday.get("day") is not None else None
            except (TypeError, ValueError):
                month = day = None
        headline = (node.get("headline") or node.get("occupation") or "").strip()
        if pub or (name and (headline or month)):
            slug = _norm_slug(str(pub))
            if slug and slug.isdigit():
                slug = ""
            out.append(CatchUpEvent(
                kind=kind,
                name=name or slug or "Unknown",
                linkedin_public_id=slug,
                linkedin_url=(f"https://www.linkedin.com/in/{slug}" if slug else ""),
                detail=headline[:240],
                month=month,
                day=day,
            ))
        for v in node.values():
            _walk(v, out, kind)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out, kind)


def _parse_month_day(text: str) -> tuple[Optional[int], Optional[int]]:
    m = re.search(r"\bon\s+([A-Za-z]{3,9})\s+(\d{1,2})\b", text or "")
    if not m:
        return None, None
    month = _MONTHS.get(m.group(1).lower()[:3])
    if not month:
        return None, None
    try:
        return month, int(m.group(2))
    except ValueError:
        return None, None


def _dedupe_events(raw: list[CatchUpEvent]) -> list[CatchUpEvent]:
    seen: dict[tuple, CatchUpEvent] = {}
    for ev in raw:
        key = (ev.linkedin_public_id or ev.name.lower(), ev.kind)
        prev = seen.get(key)
        if prev is None:
            seen[key] = ev
            continue
        if (ev.month and ev.day) and not (prev.month and prev.day):
            seen[key] = ev
        elif ev.linkedin_public_id and not prev.linkedin_public_id:
            seen[key] = ev
    return list(seen.values())


def parse_catch_up_html(html: str, *, kind: str = "birthday") -> list[CatchUpEvent]:
    """Extract Catch Up cards from LinkedIn SSR HTML (listitem rows)."""
    kind = kind if kind in CATCH_UP_KINDS else "birthday"
    if not html or not html.lstrip().startswith("<"):
        return []
    raw: list[CatchUpEvent] = []
    for chunk in re.split(r'role="listitem"', html)[1:]:
        slug_m = re.search(r"linkedin\.com/in/([^\"/?]+)", chunk)
        slug = _norm_slug(slug_m.group(1)) if slug_m else ""
        aria = re.search(r'aria-label="Message ([^:"]+):', chunk)
        name = (aria.group(1).strip() if aria else "")
        if not name:
            spans = re.findall(r"<span>([^<]{2,80})</span>", chunk)
            name = spans[0].strip() if spans else ""
        detail = ""
        month = day = None
        celebrate = re.search(r"<span>Celebrate ([^<]+)</span>", chunk)
        if celebrate:
            detail = celebrate.group(1).strip()
            month, day = _parse_month_day(detail)
        else:
            for span in re.findall(r"<span>([^<]{3,160})</span>", chunk):
                low = span.lower()
                if any(k in low for k in ("congrats", "celebrate", "anniversary", "birthday", "new role", "starting at", "finishing")):
                    detail = span.strip()
                    break
        if not slug and not name:
            continue
        raw.append(CatchUpEvent(
            kind=kind,
            name=name or slug or "Unknown",
            linkedin_public_id=slug,
            linkedin_url=(f"https://www.linkedin.com/in/{slug}" if slug else ""),
            detail=detail[:240],
            month=month,
            day=day,
        ))
    return _dedupe_events(raw)


def parse_catch_up_payload(payload: Any, *, kind: str = "birthday") -> list[CatchUpEvent]:
    """Best-effort extract people/events from Catch Up HTML or JSON payload."""
    kind = kind if kind in CATCH_UP_KINDS else "birthday"
    if payload is None:
        return []
    if isinstance(payload, str):
        if payload.lstrip().startswith("<"):
            return parse_catch_up_html(payload, kind=kind)
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    raw: list[CatchUpEvent] = []
    _walk(payload, raw, kind)
    return _dedupe_events(raw)


def _next_occurrence(month: int, day: int, *, now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    year = now.year
    try:
        due = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        due = datetime(year, month, min(day, 28), tzinfo=timezone.utc)
    if due.date() < now.date():
        due = due.replace(year=year + 1)
    return due


def _match_contact(db, user_id: int, ev: CatchUpEvent):
    from ...... import models
    slug = _norm_slug(ev.linkedin_public_id or ev.linkedin_url)
    if slug:
        for c in (db.query(models.Contact)
                    .filter(models.Contact.user_id == user_id)
                    .all()):
            curl = _norm_slug(getattr(c, "linkedin_url", "") or "")
            cpid = _norm_slug(getattr(c, "linkedin_public_id", "") or "")
            if curl == slug or cpid == slug:
                return c
    name = (ev.name or "").strip().lower()
    if not name or name == "unknown":
        return None
    matches = [c for c in db.query(models.Contact).filter_by(user_id=user_id).all()
               if (getattr(c, "name", "") or "").strip().lower() == name]
    return matches[0] if len(matches) == 1 else None


def ingest_catch_up_payload(
    db,
    user_id: int,
    payload: Any,
    *,
    kind: str = "birthday",
    commit: bool = True,
) -> dict:
    """Parse + upsert Catch Up events for matched contacts. Never raises."""
    from ....spine.memory import upsert_fact
    events = parse_catch_up_payload(payload, kind=kind)
    stats = {"parsed": len(events), "matched": 0, "stored": 0, "skipped": 0}
    fact_key = _FACT_BY_KIND.get(kind, "life_event")
    recurring = kind in _RECURRING_KINDS
    now = datetime.now(timezone.utc)
    for ev in events:
        contact = _match_contact(db, user_id, ev)
        if contact is None:
            stats["skipped"] += 1
            continue
        stats["matched"] += 1
        if kind == "birthday" and ev.month and ev.day:
            value = f"{ev.month:02d}-{ev.day:02d}"
            due = _next_occurrence(ev.month, ev.day, now=now)
            recurring = True
        elif kind == "birthday":
            # Catch Up row without explicit date — treat as birthday today.
            value = "today"
            due = now
            recurring = False
        else:
            value = (ev.detail or ev.name or kind)[:240]
            due = now if recurring else None
        try:
            upsert_fact(
                db, user_id, contact.id, fact_key, value,
                source="linkedin_catch_up",
                confidence="high" if ev.linkedin_public_id else "low",
                due_date=due,
                recurring=recurring and due is not None,
                dedup_key=kind,
                commit=False,
            )
            stats["stored"] += 1
        except Exception:  # noqa: BLE001
            stats["skipped"] += 1
    if commit:
        db.commit()
    return stats


def store_profile_birthdate(db, user_id: int, contact, profile: dict, *, commit: bool = True) -> bool:
    """Upsert birthdate from Unipile profile fetch when present."""
    from ....spine.memory import upsert_fact
    bday = (profile or {}).get("birthdate")
    if not isinstance(bday, dict):
        return False
    try:
        month, day = int(bday["month"]), int(bday["day"])
    except (KeyError, TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    upsert_fact(
        db, user_id, contact.id, "birthday", f"{month:02d}-{day:02d}",
        source="linkedin_profile", confidence="high",
        due_date=_next_occurrence(month, day, now=now),
        recurring=True, commit=commit,
    )
    return True


def _env_raw_request() -> Optional[dict]:
    raw = (os.environ.get("LINKEDIN_CATCHUP_RAW_REQUEST") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def run_catch_up_ingest(db, user, *, kind: str = "birthday") -> dict:
    """Fetch Catch Up page + ingest matched contacts into ContactFact. Best-effort."""
    from ......providers import get_provider_for_user
    acct = getattr(user, "unipile_account_id", None)
    if not acct or getattr(user, "linkedin_status", "") != "active":
        return {"ran": False, "reason": "no_linkedin"}
    kind = kind if kind in CATCH_UP_KINDS else "birthday"
    try:
        prov = get_provider_for_user(user)
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    req = _env_raw_request()
    source = "html"
    try:
        if req and req.get("request_url"):
            source = "raw"
            resp = prov.linkedin_raw(
                method=req.get("method") or "GET",
                request_url=req["request_url"],
                query_params=req.get("query_params"),
                body=req.get("body"),
                encoding=bool(req.get("encoding")),
            )
        else:
            path = CATCH_UP_PATHS.get(kind, CATCH_UP_PATHS["all"])
            resp = prov.linkedin_raw(
                method="GET",
                request_url=f"https://www.linkedin.com{path}",
            )
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    payload = unwrap_linkedin_raw(resp)
    if not payload:
        return {"ran": False, "reason": "empty_response"}
    stats = ingest_catch_up_payload(db, user.id, payload, kind=kind)
    return {"ran": True, "kind": kind, "source": source, **stats}


# ── scheduled activation (on by default, daily) ───────────────────────────────
# Claim-guarded ("catch_up_ingest" row) so one worker/replica runs it. Hooks into
# updates_scheduler.run_claimed_sweep() on each tick (Modal + in-process fallback).
_CATCH_UP_SCHEDULED_KINDS = ("birthday", "job_change", "work_anniversary", "education")
_LAST_CATCH_UP_TICK: dict = {}


def catch_up_last_tick() -> dict:
    return _LAST_CATCH_UP_TICK


def _catch_up_enabled() -> bool:
    return (os.environ.get("CATCH_UP_INGEST_ENABLED", "1").strip().lower()
            in ("1", "true", "yes", "on"))


def _catch_up_gap_seconds() -> int:
    return max(3600, int(os.environ.get("CATCH_UP_INGEST_GAP_SECONDS", "86400")))  # 24h


def run_claimed_catch_up_sweep() -> dict:
    """Claim + ingest LinkedIn Catch Up for all connected users. Fail-soft."""
    global _LAST_CATCH_UP_TICK
    stamp = datetime.now(timezone.utc).isoformat()
    if not _catch_up_enabled():
        _LAST_CATCH_UP_TICK = {"at": stamp, "ran": False, "reason": "disabled"}
        return _LAST_CATCH_UP_TICK
    from ....updates_scheduler import _claim
    if not _claim("catch_up_ingest", _catch_up_gap_seconds()):
        _LAST_CATCH_UP_TICK = {"at": stamp, "ran": False, "reason": "not due / claimed elsewhere"}
        return _LAST_CATCH_UP_TICK
    from ......db import SessionLocal
    from ...... import models
    db = SessionLocal()
    try:
        users = (db.query(models.User)
                   .filter(models.User.unipile_account_id.isnot(None),
                           models.User.unipile_account_id != "",
                           models.User.linkedin_status == "active")
                   .all())
        users_ran = stored = 0
        for user in users:
            user_stored = 0
            user_ran = False
            for kind in _CATCH_UP_SCHEDULED_KINDS:
                r = run_catch_up_ingest(db, user, kind=kind)
                if r.get("ran"):
                    user_ran = True
                    user_stored += r.get("stored", 0)
            if user_ran:
                users_ran += 1
                stored += user_stored
        _LAST_CATCH_UP_TICK = {
            "at": stamp, "ran": True,
            "result": {"users": users_ran, "stored": stored},
        }
        if users_ran:
            print(f"[catch_up.scheduler] {users_ran} user(s), {stored} fact(s) stored",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        _LAST_CATCH_UP_TICK = {"at": stamp, "ran": True, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[catch_up.scheduler] sweep failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        db.close()
    return _LAST_CATCH_UP_TICK
