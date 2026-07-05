#!/usr/bin/env python3
"""Export LinkedIn conversations the account owner has MESSAGED recently.

"Messaged" means the owner sent at least one outbound message in the window
(default: last 40 days). Cold inbound, ads, and threads you never replied to
in the window are skipped -- only conversations you actively wrote in.

Read-only: walks Unipile's chat/messages endpoints, never sends anything.

Usage:
    set -a; source .env; set +a          # load UNIPILE_DSN / UNIPILE_API_KEY
    # optional: pin the LinkedIn account (else the first LINKEDIN account is used)
    UNIPILE_ACCOUNT_ID=<linkedin-account-id> \
        python3 scripts/linkedin_recent_conversations.py --days 40

    python3 scripts/linkedin_recent_conversations.py --days 40 --json out.json

Network note: Unipile DSNs use a non-standard HTTPS port (e.g. :17054). Run
this where that port is reachable (your backend / prod env). A sandbox whose
egress proxy only allows :443 cannot reach Unipile.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.integrations.unipile_config import (  # noqa: E402
    unipile_creds,
    unipile_headers,
)


def _parse_ts(value) -> Optional[datetime]:
    """Best-effort parse of a Unipile timestamp into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch seconds vs milliseconds
        secs = value / 1000.0 if value > 1e12 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Unipile:
    def __init__(self, dsn: str, key: str, account_id: str) -> None:
        self.dsn = dsn
        self.account_id = account_id
        self.headers = {**unipile_headers(key), "accept": "application/json"}
        self._client = httpx.Client(timeout=30.0)

    def get(self, path: str, params: dict) -> dict:
        r = self._client.get(f"{self.dsn}{path}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json() if r.text else {}

    def messages(self, chat_id: str) -> list[dict]:
        """All messages for a chat, chronological, normalized."""
        out: list[dict] = []
        cursor = None
        while True:
            params = {"account_id": self.account_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = self.get(f"/api/v1/chats/{chat_id}/messages", params)
            items = data.get("items") or data.get("messages") or []
            for it in items:
                ts = _parse_ts(it.get("timestamp") or it.get("created_at"))
                out.append({
                    "direction": "outbound" if (it.get("is_sender") or it.get("from_me")) else "inbound",
                    "text": (it.get("text") or it.get("body") or "").strip(),
                    "ts": ts,
                })
            cursor = data.get("cursor")
            if not cursor:
                break
        out.sort(key=lambda m: m["ts"] or datetime.min.replace(tzinfo=timezone.utc))
        return out

    def other_attendee(self, chat_id: str) -> dict:
        data = self.get(f"/api/v1/chats/{chat_id}/attendees",
                        params={"account_id": self.account_id})
        for a in (data.get("items") or []):
            if not a.get("is_self"):
                return {
                    "name": a.get("name") or "",
                    "provider_id": a.get("provider_id") or "",
                    "profile_url": a.get("profile_url") or "",
                }
        return {}

    def public_profile(self, provider_id: str) -> dict:
        if not provider_id:
            return {}
        d = self.get(f"/api/v1/users/{provider_id}", params={"account_id": self.account_id})
        name = " ".join(x for x in [d.get("first_name"), d.get("last_name")] if x).strip()
        return {"public_identifier": d.get("public_identifier") or "", "name": name}


def resolve_linkedin_account(dsn: str, key: str) -> str:
    acct = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    if acct:
        return acct
    r = httpx.get(f"{dsn}/api/v1/accounts", headers=unipile_headers(key), timeout=30)
    r.raise_for_status()
    for a in r.json().get("items") or []:
        if "LINKEDIN" in str(a.get("type", "")).upper():
            return a["id"]
    raise SystemExit("No LinkedIn account in this Unipile workspace "
                     "(set UNIPILE_ACCOUNT_ID explicitly).")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=40,
                   help="Window: keep threads you sent a message in within this many days.")
    p.add_argument("--scan-cap", type=int, default=500,
                   help="Max chats to scan (safety bound).")
    p.add_argument("--json", metavar="PATH", help="Write full results as JSON to PATH.")
    args = p.parse_args()

    creds = unipile_creds()
    if not creds:
        raise SystemExit("UNIPILE_DSN + UNIPILE_API_KEY must both be set.")
    dsn, key = creds
    account_id = resolve_linkedin_account(dsn, key)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"LinkedIn account: {account_id}")
    print(f"Window: messages you sent on/after {cutoff.date()} (last {args.days} days)\n")

    uni = Unipile(dsn, key, account_id)
    kept: list[dict] = []
    scanned = 0
    cursor = None
    stop = False

    while not stop and scanned < args.scan_cap:
        params = {"account_id": account_id, "limit": 50}
        if cursor:
            params["cursor"] = cursor
        data = uni.get("/api/v1/chats", params)
        items = data.get("items") or []
        if not items:
            break
        for ch in items:
            scanned += 1
            chat_ts = _parse_ts(ch.get("timestamp") or ch.get("last_message_at"))
            # Chats come most-recent-first. Once a chat's latest activity is
            # older than the cutoff, nothing after it can have an in-window
            # outbound message either -> stop paging.
            if chat_ts and chat_ts < cutoff:
                stop = True
                break
            cid = ch.get("id") or ch.get("chat_id")
            if not cid:
                continue
            msgs = uni.messages(str(cid))
            my_recent = [m for m in msgs
                         if m["direction"] == "outbound" and m["ts"] and m["ts"] >= cutoff]
            if not my_recent:
                continue
            other = uni.other_attendee(str(cid))
            resolved = uni.public_profile(other.get("provider_id", ""))
            slug = resolved.get("public_identifier")
            name = other.get("name") or resolved.get("name") or "(unknown)"
            url = (f"https://www.linkedin.com/in/{slug}" if slug
                   else other.get("profile_url") or "")
            last_mine = max(m["ts"] for m in my_recent)
            kept.append({
                "name": name,
                "linkedin_url": url,
                "chat_id": str(cid),
                "my_last_message_at": last_mine.isoformat(),
                "messages_i_sent_in_window": len(my_recent),
                "messages": [
                    {"direction": m["direction"], "text": m["text"],
                     "ts": m["ts"].isoformat() if m["ts"] else None}
                    for m in msgs
                ],
            })
            print(f"  ✓ {name:<32} {url}")
            print(f"      you sent {len(my_recent)} msg(s), last {last_mine.date()}")
        cursor = data.get("cursor")
        if not cursor:
            break

    kept.sort(key=lambda c: c["my_last_message_at"], reverse=True)
    print(f"\nScanned {scanned} chats. "
          f"{len(kept)} conversation(s) you messaged in the last {args.days} days.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(kept, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
