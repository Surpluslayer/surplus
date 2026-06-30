"""
agents/relationship/whatsapp_sync.py : pull the user's REAL WhatsApp
conversations from their connected Unipile WHATSAPP account into the
relationship timeline.

This is the WhatsApp twin of email_sync. WhatsApp on Unipile is a CLOUD
messaging channel served by the SAME unified messaging API as LinkedIn DMs
(/api/v1/chats, /api/v1/chats/{id}/messages, /api/v1/chats/{id}/attendees) --
NOT a device companion. So we page the account's chats, resolve each chat's
counterpart (its phone, since WhatsApp identities ARE phone numbers), pull the
chat's messages, and ingest each one through the SHARED message sink
(routes.messages.ingest_messages) keyed by the counterpart's phone, with
channel='whatsapp', idempotent by the Unipile message id.

Unlike email_sync (which writes ONE rollup interaction per contact), this
ingests each WhatsApp message as its own timeline row -- the task spec: "ingest
each as a message keyed by the counterpart's phone, channel='whatsapp',
idempotent by message id." Reusing ingest_messages means the spine-upsert +
idempotency rules live in exactly one place.

Read-only against WhatsApp; writes only Contacts + message interactions. Never
raises out: callers get a stats dict either way (errors included), so a flaky
account can't break the connect flow that auto-kicks this.

Unipile shape notes (kept defensive -- shapes vary across their endpoints):
  * GET /api/v1/chats?account_id=...&limit=N[&cursor=...] -> {items:[{id|chat_id, ...}], cursor}
  * GET /api/v1/chats/{id}/attendees?account_id=... -> {items:[{is_self, provider_id, name, ...}]}
      For WhatsApp the non-self attendee's provider_id is the phone (often a
      "<digits>@s.whatsapp.net" JID); we strip it down to a phone handle.
  * GET /api/v1/chats/{id}/messages?account_id=...[&cursor=...]
      -> {items:[{id|message_id, text|body, is_sender|from_me, timestamp|created_at, ...}], cursor}
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

# How many chats to scan per sync (most-recent-first) and how many message
# pages to walk per chat. Bounded so a busy account can't hammer Unipile.
_MAX_CHATS = 50
_CHAT_PAGE_SIZE = 20
_MSG_PAGE_SIZE = 100
_MAX_MSG_PAGES = 3

# How many chats' fetches run concurrently. Each chat's fetch is independent,
# read-only Unipile HTTP I/O (attendees + up to _MAX_MSG_PAGES message pages),
# so a small bounded pool collapses the ~3.5 min sequential wall to well under a
# minute without hammering Unipile. The DB ingest stays single-threaded on the
# main thread AFTER the fetches return (see sync_whatsapp_contacts).
_FETCH_WORKERS = 8

_CHANNEL = "whatsapp"


class _Msg:
    """Lightweight stand-in for routes.messages.IncomingMessage. ingest_messages
    only reads attribute access (handle/name/direction/text/ts/channel/
    external_id), so a tiny holder avoids importing the pydantic model here."""

    __slots__ = ("handle", "name", "direction", "text", "ts", "channel",
                 "external_id")

    def __init__(self, *, handle, name, direction, text, ts, channel,
                 external_id):
        self.handle = handle
        self.name = name
        self.direction = direction
        self.text = text
        self.ts = ts
        self.channel = channel
        self.external_id = external_id


def _phone_from_attendee(provider_id: str) -> str:
    """Normalize a WhatsApp attendee provider_id to a phone handle. WhatsApp
    JIDs look like '14155550123@s.whatsapp.net' (or a bare number); keep the
    leading-'+'-prefixed digits so identity_keys() can hash it to a 'ph:' key."""
    pid = (provider_id or "").strip()
    if not pid:
        return ""
    local = pid.split("@", 1)[0]
    digits = "".join(ch for ch in local if ch.isdigit())
    return ("+" + digits) if digits else ""


def _msg_ts(item: dict) -> str:
    for k in ("timestamp", "created_at", "date", "sent_at"):
        v = item.get(k)
        if v:
            return str(v)
    return ""


def _default_list_chats(dsn: str, api_key: str, account_id: str,
                        cursor: Optional[str]) -> dict:
    import httpx
    params: dict[str, Any] = {"account_id": account_id, "limit": _CHAT_PAGE_SIZE}
    if cursor:
        params["cursor"] = cursor
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{dsn}/api/v1/chats",
                       headers={"X-API-KEY": api_key,
                                "accept": "application/json"},
                       params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"Unipile /chats {r.status_code}: {r.text[:200]}")
    return r.json() or {}


def _default_chat_attendees(dsn: str, api_key: str, account_id: str,
                            chat_id: str) -> dict:
    import httpx
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{dsn}/api/v1/chats/{chat_id}/attendees",
                       headers={"X-API-KEY": api_key,
                                "accept": "application/json"},
                       params={"account_id": account_id})
    if r.status_code >= 400:
        raise RuntimeError(
            f"Unipile /chats/{chat_id}/attendees {r.status_code}: {r.text[:200]}")
    return r.json() or {}


def _default_chat_messages(dsn: str, api_key: str, account_id: str,
                           chat_id: str, cursor: Optional[str]) -> dict:
    import httpx
    params: dict[str, Any] = {"account_id": account_id, "limit": _MSG_PAGE_SIZE}
    if cursor:
        params["cursor"] = cursor
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{dsn}/api/v1/chats/{chat_id}/messages",
                       headers={"X-API-KEY": api_key,
                                "accept": "application/json"},
                       params=params)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Unipile /chats/{chat_id}/messages {r.status_code}: {r.text[:200]}")
    return r.json() or {}


def _fetch_chat_batch(
    chat_id: str,
    *,
    chat_attendees: Callable[[str], dict],
    chat_messages: Callable[[str, Optional[str]], dict],
    max_msg_pages: int,
) -> list[_Msg]:
    """Fetch ONE chat's attendees + messages and build its _Msg batch.

    Pure read-only Unipile network I/O -- NO database access. Safe to run in a
    worker thread: it never touches the shared SQLAlchemy session (the caller
    ingests the returned batch single-threaded on the main thread). Returns an
    empty list for group chats, attendee-less/phone-less chats, or empty chats.

    Message pages stay sequential WITHIN a chat (each page's cursor chains off
    the previous response); the concurrency is ACROSS chats, where the win is.
    """
    # Resolve the counterpart phone (its 'ph:' identity key). 1:1 WhatsApp
    # chats have one non-self attendee. Group chats (many non-self attendees)
    # are skipped -- not a 1:1 relationship.
    att = chat_attendees(chat_id)
    others = [a for a in (att.get("items") or att.get("attendees") or [])
              if not a.get("is_self")]
    if len(others) != 1:
        return []
    other = others[0]
    phone = _phone_from_attendee(
        other.get("provider_id") or other.get("phone")
        or other.get("id") or "")
    if not phone:
        return []
    cp_name = (other.get("name") or other.get("display_name") or "").strip()

    # Page this chat's messages into a batch (built off-thread; ingested later).
    batch: list[_Msg] = []
    mcursor = None
    for _ in range(max_msg_pages):
        mpage = chat_messages(chat_id, mcursor)
        items = mpage.get("items") or mpage.get("messages") or []
        for it in items:
            text = (it.get("text") or it.get("body") or "").strip()
            ext = str(it.get("id") or it.get("message_id") or "")
            if not text or not ext:
                continue
            is_sender = bool(it.get("is_sender") or it.get("from_me"))
            batch.append(_Msg(
                handle=phone, name=cp_name,
                direction="out" if is_sender else "in",
                text=text, ts=_msg_ts(it), channel=_CHANNEL,
                external_id=ext))
        mcursor = mpage.get("cursor")
        if not mcursor or not items:
            break
    return batch


def sync_whatsapp_contacts(
    db,
    user,
    *,
    dsn: str = "",
    api_key: str = "",
    list_chats: Optional[Callable[[Optional[str]], dict]] = None,
    chat_attendees: Optional[Callable[[str], dict]] = None,
    chat_messages: Optional[Callable[[str, Optional[str]], dict]] = None,
    max_chats: int = _MAX_CHATS,
    max_msg_pages: int = _MAX_MSG_PAGES,
    fetch_workers: int = _FETCH_WORKERS,
) -> dict:
    """Sync the user's WhatsApp conversations into their relationship timeline.
    Returns aggregate stats; never raises (the connect flow auto-kicks this
    best-effort).

    The three fetchers default to live Unipile calls bound to the user's
    unipile_whatsapp_account_id; tests inject their own to drive the mapping
    without HTTP. Each message lands via the shared message sink keyed by the
    counterpart's phone, channel='whatsapp', idempotent by Unipile message id.
    """
    from ...routes.messages import ingest_messages

    stats = {"chats": 0, "appended": 0, "contacts_created": 0,
             "skipped": 0, "error": None}

    account_id = getattr(user, "unipile_whatsapp_account_id", None)
    if list_chats is None or chat_attendees is None or chat_messages is None:
        if not account_id:
            stats["error"] = "no connected whatsapp account"
            return stats
        if not (dsn and api_key):
            stats["error"] = "unipile not configured"
            return stats

    _list = list_chats or (lambda cursor: _default_list_chats(
        dsn, api_key, account_id, cursor))
    _atts = chat_attendees or (lambda cid: _default_chat_attendees(
        dsn, api_key, account_id, cid))
    _msgs = chat_messages or (lambda cid, cursor: _default_chat_messages(
        dsn, api_key, account_id, cid, cursor))

    try:
        # 1) PAGE chat ids (cheap, sequential -- cursor chains; few pages).
        chat_ids: list[str] = []
        cursor = None
        while len(chat_ids) < max_chats:
            page = _list(cursor)
            chats = page.get("items") or page.get("chats") or []
            if not chats:
                break
            for ch in chats:
                if len(chat_ids) >= max_chats:
                    break
                chat_id = str(ch.get("id") or ch.get("chat_id") or "")
                if chat_id:
                    chat_ids.append(chat_id)
            cursor = page.get("cursor")
            if not cursor:
                break

        # 2) FETCH each chat's attendees + messages CONCURRENTLY (read-only
        # Unipile HTTP, no DB). A bounded pool keeps the load civil. Worker
        # threads never touch `db` -- they only build _Msg batches.
        workers = max(1, min(fetch_workers, len(chat_ids))) if chat_ids else 1
        results: list[tuple[str, list[_Msg]]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _fetch_chat_batch, cid,
                    chat_attendees=_atts, chat_messages=_msgs,
                    max_msg_pages=max_msg_pages,
                ): cid
                for cid in chat_ids
            }
            for fut in futs:
                cid = futs[fut]
                try:
                    results.append((cid, fut.result()))
                except Exception as exc:  # noqa: BLE001 : one bad chat != whole sync
                    stats["error"] = f"{type(exc).__name__}: {exc}"

        # 3) INGEST sequentially on the MAIN THREAD -- the only writer to the
        # shared SQLAlchemy session. Preserves idempotency (skip-by-message-id
        # lives in ingest_messages) and keeps DB access single-threaded.
        for _cid, batch in results:
            if not batch:
                continue
            s = ingest_messages(db, user, batch)
            stats["chats"] += 1
            stats["appended"] += s.get("appended", 0)
            stats["contacts_created"] += s.get("contacts_created", 0)
            stats["skipped"] += s.get("skipped", 0)
    except Exception as exc:  # noqa: BLE001 : a flaky account must not 500
        stats["error"] = f"{type(exc).__name__}: {exc}"

    return stats
