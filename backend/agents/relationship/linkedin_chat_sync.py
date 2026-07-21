"""
agents/relationship/linkedin_chat_sync.py : pull the user's REAL LinkedIn DM
conversations from their connected Unipile LINKEDIN seat into the relationship
timeline.

This is the LinkedIn twin of whatsapp_sync. LinkedIn DMs live on the SAME
unified Unipile messaging API (/api/v1/chats, /api/v1/chats/{id}/messages,
/api/v1/chats/{id}/attendees) already used by the WhatsApp sync -- the
account_id scopes which network's chats come back. So we page the LinkedIn
account's chats, resolve each chat's counterpart to a public LinkedIn URL
(attendee provider_id -> /api/v1/users/{provider_id} -> public_identifier,
the same resolution import_conversation_contacts uses), find-or-create the
Contact on the li: identity key, and land each message through the shared
message sink rules (routes.messages.append_message_for_contact), with
channel='linkedin', idempotent by the Unipile message id.

Because rows land with source_type='linkedin' (a MESSAGING_CHANNELS member),
they flow straight into the thread the drafter reads (thread_from_timeline).

Incremental: users.linkedin_chat_synced_at is the per-user watermark. When set,
a sync skips chats whose last activity predates it and stops paging a chat's
messages once it walks past it (message pages come newest-first). The watermark
is stamped to the sync's START time on a clean run, so anything that arrived
mid-sync is re-scanned next time (dedup-by-message-id makes the overlap free).

Read-only against LinkedIn; writes only Contacts + message interactions. Never
raises out: callers get a stats dict either way (errors included), so a flaky
account can't break the flow that auto-kicks this.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

# The Unipile /chats and /chats/{id}/messages fetchers are network-shape
# identical across WhatsApp and LinkedIn (same unified API), so reuse the
# proven ones instead of duplicating the httpx plumbing.
from .whatsapp_sync import (_Msg, _default_chat_messages, _default_list_chats,
                            _msg_ts)

# How many chats to scan per sync (most-recent-first) and how many message
# pages to walk per chat. LinkedIn inboxes run far deeper than WhatsApp, so the
# chat cap is higher; still bounded so a busy account can't hammer Unipile.
_MAX_CHATS = 500
_MSG_PAGE_SIZE = 100
_MAX_MSG_PAGES = 3

# How many chats' fetches run concurrently. Each chat's fetch is independent,
# read-only Unipile HTTP I/O (attendees + profile resolve + message pages), so
# a small bounded pool collapses the sequential wall-clock without hammering
# Unipile. The DB ingest stays single-threaded on the main thread AFTER the
# fetches return (see sync_linkedin_chats).
_FETCH_WORKERS = 8

_CHANNEL = "linkedin"

# Unipile returns a placeholder for media/voice/etc. it can't render as text
# ("Unipile cannot display this type of message yet"). Same noise filter the
# drafter's gather path applies -- don't even store it.
_PLACEHOLDER = "unipile cannot display"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_ts(raw: str) -> Optional[datetime]:
    from ...integrations.sync_common import parse_iso
    return parse_iso(raw)


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


def _default_resolve_profile(dsn: str, api_key: str, account_id: str,
                             provider_id: str) -> dict:
    """provider_id (LinkedIn member id) -> profile dict with public_identifier.
    Best-effort: {} on any failure (the attendee's profile_url is the fallback)."""
    import httpx
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(f"{dsn}/api/v1/users/{provider_id}",
                           headers={"X-API-KEY": api_key,
                                    "accept": "application/json"},
                           params={"account_id": account_id})
        if r.status_code >= 400:
            return {}
        return r.json() or {}
    except Exception:  # noqa: BLE001 : profile resolve is best-effort
        return {}


def _peer_of(att: dict) -> Optional[dict]:
    """The single non-self attendee of a 1:1 chat, or None (group/empty)."""
    others = [a for a in (att.get("items") or att.get("attendees") or [])
              if not a.get("is_self")]
    if len(others) != 1:
        return None
    return others[0]


def _fetch_chat_batch(
    chat_id: str,
    *,
    chat_attendees: Callable[[str], dict],
    chat_messages: Callable[[str, Optional[str]], dict],
    resolve_profile: Callable[[str], dict],
    max_msg_pages: int,
    since: Optional[datetime],
) -> Optional[tuple[dict, list[_Msg]]]:
    """Fetch ONE chat's peer + messages: (peer_info, batch) or None to skip.

    Pure read-only Unipile network I/O -- NO database access. Safe to run in a
    worker thread: it never touches the shared SQLAlchemy session (the caller
    ingests the returned batch single-threaded on the main thread). Skips group
    chats, unresolvable peers, LinkedIn's own system account, and media
    placeholders. `since` (the incremental watermark) stops the message paging
    early: pages come newest-first, so once a message predates it the rest of
    the chat is already ingested.
    """
    other = _peer_of(chat_attendees(chat_id))
    if other is None:
        return None
    pid = str(other.get("provider_id") or other.get("id") or "")
    if not pid:
        return None

    # Resolve the clean public slug (the li: identity the whole app keys on).
    resolved = resolve_profile(pid)
    slug = (resolved.get("public_identifier") or "").strip()
    name = (other.get("name") or other.get("display_name") or "").strip()
    if not name:
        parts = [resolved.get("first_name"), resolved.get("last_name")]
        name = " ".join(x for x in parts if x).strip()
    url = (f"https://www.linkedin.com/in/{slug}" if slug
           else (other.get("profile_url") or "").strip())
    if not url:
        return None
    # Skip LinkedIn's own system/notification account (platform messages, not
    # a real person): name == "LinkedIn" or a numeric member-id slug.
    if name.strip().lower() == "linkedin" or (slug and slug.isdigit()):
        return None
    peer = {"name": name, "linkedin_url": url, "linkedin_public_id": slug,
            "headline": (resolved.get("headline") or "").strip()}

    # Page this chat's messages into a batch (built off-thread; ingested later).
    batch: list[_Msg] = []
    mcursor = None
    for _ in range(max_msg_pages):
        mpage = chat_messages(chat_id, mcursor)
        items = mpage.get("items") or mpage.get("messages") or []
        past_watermark = False
        for it in items:
            text = (it.get("text") or it.get("body") or "").strip()
            ext = str(it.get("id") or it.get("message_id") or "")
            if not text or not ext:
                continue
            if _PLACEHOLDER in text.lower():
                continue  # media/voice placeholder, noise not conversation
            ts_raw = _msg_ts(it)
            if since is not None:
                when = _aware(_parse_ts(ts_raw))
                if when is not None and when < since:
                    past_watermark = True
                    continue  # older than the watermark: already ingested
            is_sender = bool(it.get("is_sender") or it.get("from_me"))
            batch.append(_Msg(
                handle=url, name=name,
                direction="out" if is_sender else "in",
                text=text, ts=ts_raw, channel=_CHANNEL,
                external_id=ext))
        mcursor = mpage.get("cursor")
        if not mcursor or not items or past_watermark:
            break
    return peer, batch


def _find_or_create_linkedin_contact(db, user, peer: dict, stats: dict):
    """Find-or-create the Contact for a chat peer on the li: identity key --
    the SAME scheme import_conversation_contacts and link_contact use (never a
    new one). Returns the Contact or None when the URL yields no strong key."""
    from ... import models
    from .enrichment_cache import identity_keys
    from . import identity as _identity

    keys = identity_keys(email="", linkedin_url=peer["linkedin_url"])
    if not keys:
        return None
    primary = keys[0]
    # Look up by the primary li: key first, then by ANY strong identity the peer
    # implies -- so a LinkedIn mint links to a contact already created from email
    # (once that person's linkedin is known) rather than forking a duplicate.
    idents = _identity.strong_identities(
        linkedin_url=peer["linkedin_url"],
        linkedin_public_id=peer.get("linkedin_public_id") or "")
    contact = (db.query(models.Contact)
               .filter_by(user_id=user.id, primary_identity_key=primary)
               .first())
    if contact is None:
        contact = _identity.lookup_contact_by_identities(
            db, user_id=user.id, identities=idents)
    if contact is None:
        contact = models.Contact(
            user_id=user.id, primary_identity_key=primary,
            name=peer["name"] or None,
            linkedin_url=peer["linkedin_url"],
            linkedin_public_id=peer["linkedin_public_id"] or None,
            headline=peer["headline"] or None,
        )
        db.add(contact)
        db.flush()
        stats["contacts_created"] += 1
    else:
        # Enrich, never clobber: fill blanks the chat can answer.
        if not contact.name and peer["name"]:
            contact.name = peer["name"]
        if not contact.linkedin_url:
            contact.linkedin_url = peer["linkedin_url"]
        if not contact.linkedin_public_id and peer["linkedin_public_id"]:
            contact.linkedin_public_id = peer["linkedin_public_id"]
        if not contact.headline and peer["headline"]:
            contact.headline = peer["headline"]
    _identity.register_identities(db, contact=contact, identities=idents,
                                  source="linkedin_profile", primary_key=primary)
    return contact


def _ingest_chat_batch(session_factory, user_id: int, peer: dict,
                       batch: list, stats: dict) -> bool:
    """Ingest ONE fetched chat in its OWN short-lived session: open, write,
    commit, close. Returns True when the chat landed (the caller counts it in
    stats['chats']). Per-chat sessions mean a pooled DB connection is never
    held longer than one chat's writes -- the multi-minute network fetching
    happens with no connection checked out at all. Partial progress persists
    (each chat commits); dedup-by-message-id makes any re-run overlap free."""
    from ... import models
    from ...routes.messages import append_message_for_contact

    s = session_factory()
    try:
        u = s.get(models.User, user_id)
        if u is None:
            return False
        contact = _find_or_create_linkedin_contact(s, u, peer, stats)
        if contact is None:
            s.rollback()
            return False
        for m in batch:
            append_message_for_contact(s, u, contact, m, stats)
        s.commit()
        return True
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def sync_linkedin_chats(
    db,
    user,
    *,
    dsn: str = "",
    api_key: str = "",
    list_chats: Optional[Callable[[Optional[str]], dict]] = None,
    chat_attendees: Optional[Callable[[str], dict]] = None,
    chat_messages: Optional[Callable[[str, Optional[str]], dict]] = None,
    resolve_profile: Optional[Callable[[str], dict]] = None,
    max_chats: int = _MAX_CHATS,
    max_msg_pages: int = _MAX_MSG_PAGES,
    fetch_workers: int = _FETCH_WORKERS,
    incremental: bool = True,
    session_factory: Optional[Callable[[], object]] = None,
) -> dict:
    """Sync the user's LinkedIn DM conversations into their relationship
    timeline. Returns aggregate stats; never raises (callers auto-kick this
    best-effort).

    The fetchers default to live Unipile calls bound to the user's
    unipile_account_id (the LinkedIn seat); tests inject their own to drive the
    mapping without HTTP. Each message lands via the shared message-sink rules
    keyed by the peer's li: identity, channel='linkedin', idempotent by Unipile
    message id. `incremental` uses/advances users.linkedin_chat_synced_at.

    Session lifecycle contract: `db` is used only for the brief input read
    (account, status, watermark) and the final watermark stamp. Its pooled
    connection is RELEASED (via commit) before the network phases, so this
    multi-minute sync never pins a connection while it talks to Unipile (the
    2026-07-01 QueuePool exhaustion). Each chat's ingest runs in its own
    short-lived session from `session_factory` (default: a sessionmaker on
    db's bind), committed per chat."""
    stats = {"chats": 0, "appended": 0, "contacts_created": 0,
             "skipped": 0, "error": None}

    account_id = getattr(user, "unipile_account_id", None)
    if not account_id:
        stats["error"] = "no connected linkedin account"
        return stats
    if getattr(user, "linkedin_status", "") != "active":
        stats["error"] = "linkedin account not active"
        return stats
    injected = not (list_chats is None or chat_attendees is None
                    or chat_messages is None or resolve_profile is None)
    if not injected and not (dsn and api_key):
        stats["error"] = "unipile not configured"
        return stats

    _list = list_chats or (lambda cursor: _default_list_chats(
        dsn, api_key, account_id, cursor))
    _atts = chat_attendees or (lambda cid: _default_chat_attendees(
        dsn, api_key, account_id, cid))
    _msgs = chat_messages or (lambda cid, cursor: _default_chat_messages(
        dsn, api_key, account_id, cid, cursor))
    _prof = resolve_profile or (lambda pid: _default_resolve_profile(
        dsn, api_key, account_id, pid))

    since = _aware(getattr(user, "linkedin_chat_synced_at", None)) \
        if incremental else None
    started_at = datetime.now(timezone.utc)
    user_id = user.id
    if session_factory is None:
        from sqlalchemy.orm import sessionmaker
        session_factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    # Inputs are captured; RELEASE db's pooled connection before the long
    # network phases. Commit (not rollback) so any caller-pending work is
    # preserved rather than discarded; by contract nothing of ours is staged.
    db.commit()

    try:
        # 1) PAGE chat ids (cheap, sequential -- cursor chains; few pages).
        # Chats come most-recent-activity-first; with a watermark we stop as
        # soon as a chat's last activity predates it (everything after is older).
        chat_ids: list[str] = []
        cursor = None
        stale = False
        while len(chat_ids) < max_chats and not stale:
            page = _list(cursor)
            chats = page.get("items") or page.get("chats") or []
            if not chats:
                break
            for ch in chats:
                if len(chat_ids) >= max_chats:
                    break
                last = _aware(_parse_ts(str(
                    ch.get("timestamp") or ch.get("last_message_at") or "")))
                if since is not None and last is not None and last < since:
                    stale = True
                    break
                chat_id = str(ch.get("id") or ch.get("chat_id") or "")
                if chat_id:
                    chat_ids.append(chat_id)
            cursor = page.get("cursor")
            if not cursor:
                break
        print(f"  [linkedin_sync] user={user_id} scanning {len(chat_ids)} chats"
              + (f" (since {since.isoformat()})" if since else ""), flush=True)

        # 2) FETCH each chat's peer + messages CONCURRENTLY (read-only Unipile
        # HTTP, no DB). A bounded pool keeps the load civil. Worker threads
        # never touch `db` -- they only build (peer, batch) tuples.
        workers = max(1, min(fetch_workers, len(chat_ids))) if chat_ids else 1
        results: list[tuple[dict, list[_Msg]]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _fetch_chat_batch, cid,
                    chat_attendees=_atts, chat_messages=_msgs,
                    resolve_profile=_prof, max_msg_pages=max_msg_pages,
                    since=since,
                ): cid
                for cid in chat_ids
            }
            for fut in futs:
                try:
                    res = fut.result()
                    if res is not None:
                        results.append(res)
                except Exception as exc:  # noqa: BLE001 : one bad chat != whole sync
                    stats["error"] = f"{type(exc).__name__}: {exc}"

        # 3) INGEST sequentially on the MAIN THREAD, one SHORT-LIVED session
        # per chat (open, write, commit, close) -- no pooled connection is
        # held across chats, and none was held during the fetches above.
        # Idempotency is preserved (skip-by-message-id lives in
        # append_message_for_contact) and DB access stays single-threaded.
        for peer, batch in results:
            if not batch:
                continue
            if _ingest_chat_batch(session_factory, user_id, peer, batch, stats):
                stats["chats"] += 1
        # Advance the watermark only on a CLEAN pass -- a partial/errored run
        # must re-scan its window next time (dedup makes the overlap free).
        # This is the one write on `db` (short: set + commit).
        if incremental and stats["error"] is None \
                and hasattr(user, "linkedin_chat_synced_at"):
            user.linkedin_chat_synced_at = started_at
            db.commit()
        print(f"  [linkedin_sync] user={user_id} done: {stats}", flush=True)
    except Exception as exc:  # noqa: BLE001 : a flaky account must not 500
        stats["error"] = f"{type(exc).__name__}: {exc}"

    return stats


def run_linkedin_chat_sync_job(db, user_id: int, incremental: bool = True) -> dict:
    """Detached sync body (run via jobs.run_detached, the same durable path the
    connect-time seeds use: Modal run_detached_job when USE_MODAL, else a local
    daemon thread that owns its session). Top-level so the Modal path can
    resolve it by dotted path; `db` is the run_detached-owned session."""
    from ... import models
    from ...jobs import _unipile_env

    dsn, api_key = _unipile_env()
    user = db.get(models.User, user_id)
    if user is None:
        print(f"  [linkedin_sync] user {user_id} NOT FOUND", flush=True)
        return {"user_id": user_id, "error": "user not found"}
    stats = sync_linkedin_chats(db, user, dsn=dsn, api_key=api_key,
                                incremental=incremental)
    return {"user_id": user_id, **stats}


def dispatch_linkedin_chat_sync(user_id: int, incremental: bool = True) -> str:
    """Dispatch a user's LinkedIn chat sync DURABLY, off the request lifecycle.
    Returns 'modal' or 'local'. Idempotent, so a belt-and-suspenders double-run
    is safe. Never raises into the caller."""
    from ...jobs import run_detached
    return run_detached(run_linkedin_chat_sync_job, user_id,
                        incremental=incremental, prefer_modal=True)
