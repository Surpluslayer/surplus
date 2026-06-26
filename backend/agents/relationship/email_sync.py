"""
agents/email_sync.py : pull who the user ACTUALLY corresponds with from their
connected mailbox (Unipile GOOGLE/OUTLOOK seat) into the relationship spine.

This is the email twin of the LinkedIn import promise on the Integrations
tile: "pulls who you actually correspond with — and when you last talked."

How it works
────────────
  1. Page GET /api/v1/emails (meta_only — headers, never bodies) for the
     user's email account, newest first.
  2. For each mail, derive direction (sent vs received) and the human
     counterpart(s). Bulk mail is skipped two ways:
       - junk senders (no-reply@, notifications@, newsletters …)
       - fan-out mail (more than _MAX_RECIPIENTS recipients = an announcement,
         not a correspondence)
  3. Aggregate per counterpart address: name, last inbound, last outbound,
     message counts.
  4. Upsert each counterpart into the Contact spine via the SAME identity
     scheme the rest of the app uses (identity_keys -> "em:<salted hash>"),
     and record ONE rollup RelationshipInteraction per contact carrying the
     correspondence stats (source_type="email_sync"). Re-syncs update that
     rollup in place instead of appending — the timeline shows one truthful
     "email thread" touch per person, stamped with the LAST real exchange.

Read-only against the mailbox; writes only Contacts + the rollup interaction.
Never raises out: callers get a stats dict either way (errors included), so
a flaky mailbox can't break the connect flow that auto-kicks this.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ... import models

# Senders that are machines, not relationships. Matched as a PREFIX of the
# local part (so "noreply+tag@x" is caught) after lowercasing.
_JUNK_LOCALPART_PREFIXES = (
    "no-reply", "noreply", "no_reply", "do-not-reply", "donotreply",
    "notifications", "notification", "notify", "mailer-daemon", "postmaster",
    "bounce", "newsletter", "news", "marketing", "promo", "offers", "deals",
    "updates", "alerts", "alert", "digest", "info", "hello", "support",
    "help", "billing", "receipts", "receipt", "invoice", "orders",
    "accounts", "account", "admin", "team", "careers", "jobs", "security",
    "feedback", "calendar-notification", "drive-shares", "comments",
)
# More than this many recipients = an announcement / thread blast, not a
# 1:1 correspondence worth a contact row.
_MAX_RECIPIENTS = 5
# How many mails to scan per sync (newest first). 4 pages × 100 covers weeks
# of a busy inbox without hammering Unipile.
_PAGE_SIZE = 100
_MAX_PAGES = 4

_ROLLUP_SOURCE = "email_sync"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_date(raw: Any) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_junk_address(addr: str) -> bool:
    """True for machine senders we never want as contacts."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return True
    local = addr.split("@", 1)[0]
    return any(local.startswith(p) for p in _JUNK_LOCALPART_PREFIXES)


def _attendee(a: Any) -> tuple[str, str]:
    """(address, display_name) from a Unipile attendee object."""
    if not isinstance(a, dict):
        return "", ""
    addr = (a.get("identifier") or "").strip().lower()
    name = (a.get("display_name") or "").strip()
    return addr, name


def counterparts_of(mail: dict, own_address: str) -> tuple[str, list[tuple[str, str]]]:
    """('in'|'out'|'skip', [(address, name), ...]) for one mail.

    Outbound (the host wrote it): counterparts are the recipients.
    Inbound: the counterpart is the sender. Fan-out mail and junk senders
    come back as 'skip'.
    """
    own = (own_address or "").strip().lower()
    from_addr, from_name = _attendee(mail.get("from_attendee"))
    tos = [_attendee(a) for a in (mail.get("to_attendees") or [])]
    role = (mail.get("role") or "").lower()

    outbound = (own and from_addr == own) or role == "sent"
    if outbound:
        if len(tos) > _MAX_RECIPIENTS:
            return "skip", []
        people = [(a, n) for a, n in tos
                  if a and a != own and not is_junk_address(a)]
        return ("out", people) if people else ("skip", [])

    # Inbound : sender is the counterpart. A blast TO many people is skipped
    # too — being cc'd on an announcement isn't a relationship.
    if not from_addr or from_addr == own or is_junk_address(from_addr):
        return "skip", []
    if len(tos) > _MAX_RECIPIENTS:
        return "skip", []
    return "in", [(from_addr, from_name)]


def _default_fetch_page(dsn: str, api_key: str, account_id: str,
                        cursor: Optional[str]) -> dict:
    """GET one page of mail metadata. Plain httpx; meta_only keeps bodies
    (and their PII bulk) out of the wire entirely."""
    import httpx
    params: dict[str, Any] = {"account_id": account_id,
                              "limit": _PAGE_SIZE, "meta_only": "true"}
    if cursor:
        params["cursor"] = cursor
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{dsn}/api/v1/emails",
                       headers={"X-API-KEY": api_key,
                                "accept": "application/json"},
                       params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"Unipile /emails {r.status_code}: {r.text[:200]}")
    return r.json() or {}


def sync_email_contacts(
    db,
    user,
    *,
    dsn: str,
    api_key: str,
    fetch_page: Optional[Callable[[Optional[str]], dict]] = None,
    max_pages: int = _MAX_PAGES,
) -> dict:
    """Sync the user's mailbox into their Contact spine. Returns stats; never
    raises (the connect flow auto-kicks this best-effort)."""
    from .relationships import _clean  # same cleaners as the LinkedIn spine
    from ...triage.enrichment_cache import identity_keys

    stats = {"scanned": 0, "people": 0, "contacts_created": 0,
             "contacts_updated": 0, "skipped_junk": 0, "error": None}
    account_id = getattr(user, "unipile_email_account_id", None)
    if not account_id:
        stats["error"] = "no connected email account"
        return stats
    own = (getattr(user, "email_account_address", "") or "").strip().lower()
    fetcher = fetch_page or (
        lambda cursor: _default_fetch_page(dsn, api_key, account_id, cursor))

    # ── 1+2+3 : page + aggregate per counterpart ────────────────────────────
    agg: dict[str, dict] = {}
    try:
        cursor = None
        for _ in range(max_pages):
            page = fetcher(cursor)
            items = page.get("items") or []
            for mail in items:
                stats["scanned"] += 1
                direction, people = counterparts_of(mail, own)
                if direction == "skip":
                    stats["skipped_junk"] += 1
                    continue
                when = _parse_date(mail.get("date"))
                for addr, name in people:
                    a = agg.setdefault(addr, {
                        "name": "", "last_in": None, "last_out": None,
                        "n_in": 0, "n_out": 0,
                    })
                    if name and not a["name"]:
                        a["name"] = name
                    key = "n_out" if direction == "out" else "n_in"
                    a[key] += 1
                    tkey = "last_out" if direction == "out" else "last_in"
                    if when and (a[tkey] is None or when > a[tkey]):
                        a[tkey] = when
            cursor = page.get("cursor")
            if not cursor or not items:
                break
    except Exception as exc:  # noqa: BLE001 : a flaky mailbox must not 500
        stats["error"] = f"{type(exc).__name__}: {exc}"
        if not agg:
            return stats  # nothing usable; partial pages still get written

    # ── 4 : upsert the spine ────────────────────────────────────────────────
    for addr, a in agg.items():
        keys = identity_keys(email=addr, linkedin_url="")
        if not keys:
            continue
        stats["people"] += 1
        primary = keys[0]
        contact = (db.query(models.Contact)
                   .filter_by(user_id=user.id, primary_identity_key=primary)
                   .first())
        if contact is None:
            contact = models.Contact(
                user_id=user.id, primary_identity_key=primary,
                name=_clean(a["name"]), email=addr,
            )
            db.add(contact)
            db.flush()
            stats["contacts_created"] += 1
        else:
            # Enrich, never clobber : fill blanks the mailbox can answer.
            if not contact.email:
                contact.email = addr
            if not contact.name and a["name"]:
                contact.name = _clean(a["name"])
            stats["contacts_updated"] += 1

        last_touch = max(filter(None, [a["last_in"], a["last_out"]]),
                         default=None)
        their_turn = bool(a["last_in"] and (
            a["last_out"] is None or a["last_in"] > a["last_out"]))
        summary = (f"Email thread · {a['n_in'] + a['n_out']} messages "
                   f"({a['n_in']} from them, {a['n_out']} from you)"
                   + (" · last word was theirs" if their_turn else ""))

        # One rollup interaction per contact, updated in place on re-sync so
        # the timeline carries a single truthful "email thread" touch.
        rollup = (db.query(models.RelationshipInteraction)
                  .filter_by(actor_user_id=user.id, contact_id=contact.id,
                             source_type=_ROLLUP_SOURCE)
                  .first())
        if rollup is None:
            rollup = models.RelationshipInteraction(
                actor_user_id=user.id, contact_id=contact.id,
                source_type=_ROLLUP_SOURCE, interaction_type="email_thread",
                direction="in" if their_turn else "out",
                title="Email correspondence",
            )
            db.add(rollup)
        rollup.summary = summary
        rollup.direction = "in" if their_turn else "out"
        if last_touch:
            rollup.occurred_at = last_touch
        rollup.meta_json = json.dumps({
            "n_in": a["n_in"], "n_out": a["n_out"], "address": addr,
            "last_in": a["last_in"].isoformat() if a["last_in"] else None,
            "last_out": a["last_out"].isoformat() if a["last_out"] else None,
        })

    db.commit()
    return stats


# ─── Thread-level pull/push support (host-confirmed thread linking) ──────────
# The host manually confirms "this is my thread with this person"
# (Contact.email_thread_id). These helpers list the candidates for that
# confirmation and read the linked thread back for the timeline / composer.

def _mail_brief(mail: dict, own_address: str) -> dict:
    from_addr, from_name = _attendee(mail.get("from_attendee"))
    own = (own_address or "").strip().lower()
    return {
        "provider_id": mail.get("provider_id") or mail.get("id"),
        "thread_id": mail.get("thread_id"),
        "subject": mail.get("subject") or "",
        "date": mail.get("date"),
        "direction": "out" if (mail.get("role") == "sent"
                               or (own and from_addr == own)) else "in",
        "from_address": from_addr,
        "from_name": from_name,
        "body": mail.get("body") or "",
    }


def list_threads_for_address(*, dsn: str, api_key: str, account_id: str,
                             address: str, own_address: str = "",
                             fetch: Optional[Callable[..., dict]] = None) -> list[dict]:
    """Candidate threads with `address`, newest activity first — what the
    host picks from when confirming the thread for a contact."""
    import httpx

    def _default(**params):
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{dsn}/api/v1/emails",
                           headers={"X-API-KEY": api_key,
                                    "accept": "application/json"},
                           params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"Unipile /emails {r.status_code}: {r.text[:200]}")
        return r.json() or {}

    page = (fetch or _default)(account_id=account_id, any_email=address,
                               limit=100, meta_only="true")
    threads: dict[str, dict] = {}
    for mail in page.get("items") or []:
        b = _mail_brief(mail, own_address)
        tid = b["thread_id"]
        if not tid:
            continue
        t = threads.setdefault(tid, {"thread_id": tid, "subject": b["subject"],
                                     "last_date": None, "n": 0})
        t["n"] += 1
        when = _parse_date(b["date"])
        if when and (t["last_date"] is None or when > t["last_date"]):
            t["last_date"] = when
            t["subject"] = b["subject"] or t["subject"]
    out = sorted(threads.values(),
                 key=lambda t: t["last_date"] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    for t in out:
        t["last_date"] = t["last_date"].isoformat() if t["last_date"] else None
    return out


def thread_messages(*, dsn: str, api_key: str, account_id: str,
                    thread_id: str, own_address: str = "",
                    with_bodies: bool = False,
                    fetch: Optional[Callable[..., dict]] = None) -> list[dict]:
    """The linked thread's messages, oldest first. `with_bodies=False` keeps
    it to headers (timeline view); True pulls bodies (composer grounding).
    The LAST message's provider_id + subject are what a reply must use
    (reply_to + 'Re:' subject) to stay in-thread."""
    import httpx

    def _default(**params):
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{dsn}/api/v1/emails",
                           headers={"X-API-KEY": api_key,
                                    "accept": "application/json"},
                           params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"Unipile /emails {r.status_code}: {r.text[:200]}")
        return r.json() or {}

    params = {"account_id": account_id, "thread_id": thread_id, "limit": 100}
    if not with_bodies:
        params["meta_only"] = "true"
    page = (fetch or _default)(**params)
    msgs = [_mail_brief(m, own_address) for m in page.get("items") or []]
    msgs.sort(key=lambda m: _parse_date(m["date"])
              or datetime.min.replace(tzinfo=timezone.utc))
    return msgs


def format_email_html(text: str, to_first: str = "", host_first: str = "") -> str:
    """Shape a DM-style draft into a proper email: greeting line, body
    paragraphs, sign-off with the host's name. An inline 'Hey Jia, ...'
    opener is lifted onto its own line; newlines become <br> (Unipile body
    is HTML, where plain \n collapses into one run-on line)."""
    import re
    body = (text or "").strip()
    m = re.match(r"^(hi|hey|hello)[ ,]+([A-Za-z'\-]+)[,!.]?\s*", body, re.I)
    if m:
        greeting = f"{m.group(1).capitalize()} {m.group(2)},"
        body = body[m.end():].strip()
    else:
        greeting = f"Hi {to_first.strip()}," if to_first.strip() else "Hi,"
    if body:
        body = body[0].upper() + body[1:]
    paras = "<br><br>".join(p.strip().replace("\n", "<br>")
                            for p in body.split("\n\n") if p.strip()) or body
    sig = f"Best,<br>{host_first.strip()}" if host_first.strip() else "Best,"
    return f"{greeting}<br><br>{paras}<br><br>{sig}"
