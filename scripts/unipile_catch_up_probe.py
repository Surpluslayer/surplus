#!/usr/bin/env python3
"""Probe LinkedIn Catch Up via Unipile raw route.

Default path: fetch the Catch Up HTML page and parse SSR listitem cards (names,
LinkedIn slugs, event text). No DevTools capture required.

Optional: set LINKEDIN_CATCHUP_RAW_REQUEST for a captured SDUI/XHR JSON endpoint.

Usage:
  set -a; source .env; set +a
  UNIPILE_ACCOUNT_ID=<live-linkedin-id> python3 scripts/unipile_catch_up_probe.py --html-shell
  python3 scripts/unipile_catch_up_probe.py --html-shell --kind job_change
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.relationship.pipeline.context.ingest.catch_up import (
    CATCH_UP_PATHS,
    ingest_catch_up_payload,
    parse_catch_up_payload,
    unwrap_linkedin_raw,
)
from backend.providers import get_provider


def _resolve_account_id() -> str:
    acct = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    if acct:
        return acct
    import httpx
    dsn = (os.environ.get("UNIPILE_DSN") or "").strip().rstrip("/")
    if dsn and not dsn.startswith("http"):
        dsn = f"https://{dsn}"
    key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
    r = httpx.get(f"{dsn}/api/v1/accounts", headers={"X-API-KEY": key}, timeout=20)
    r.raise_for_status()
    for a in r.json().get("items") or []:
        if "LINKEDIN" in str(a.get("type", "")).upper():
            return a["id"]
    raise SystemExit("No LinkedIn account in Unipile workspace")


def main() -> None:
    p = argparse.ArgumentParser(description="Probe LinkedIn Catch Up via Unipile")
    p.add_argument("--kind", default="birthday", choices=list(CATCH_UP_PATHS))
    p.add_argument("--ingest", action="store_true", help="Parse only (no DB)")
    p.add_argument("--html-shell", action="store_true",
                   help="Fetch Catch Up HTML via raw route (usually SDUI shell only)")
    args = p.parse_args()

    acct = _resolve_account_id()
    os.environ["UNIPILE_ACCOUNT_ID"] = acct
    prov = get_provider()

    req_env = (os.environ.get("LINKEDIN_CATCHUP_RAW_REQUEST") or "").strip()
    if req_env:
        req = json.loads(req_env)
        print("Using LINKEDIN_CATCHUP_RAW_REQUEST →", req.get("request_url", "")[:80])
        resp = prov.linkedin_raw(
            method=req.get("method") or "GET",
            request_url=req["request_url"],
            query_params=req.get("query_params"),
            body=req.get("body"),
            encoding=bool(req.get("encoding")),
        )
    elif args.html_shell:
        path = CATCH_UP_PATHS[args.kind]
        print(f"Fetching HTML shell {path} (card data usually NOT in this response)")
        resp = prov.linkedin_raw(
            method="GET",
            request_url=f"https://www.linkedin.com{path}",
        )
    else:
        print("Set LINKEDIN_CATCHUP_RAW_REQUEST from DevTools, or pass --html-shell.")
        print("Catch Up paths:", json.dumps(CATCH_UP_PATHS, indent=2))
        raise SystemExit(1)

    if not resp:
        print("Empty Unipile response — check UNIPILE_ACCOUNT_ID (stale → 404) and creds.")
        raise SystemExit(1)

    payload = unwrap_linkedin_raw(resp)
    if isinstance(payload, str) and payload.lstrip().startswith("<"):
        print(f"Got HTML ({len(payload)} chars) — parsing SSR listitem cards.")

    events = parse_catch_up_payload(payload, kind=args.kind)
    print(f"Parsed {len(events)} event(s):")
    for ev in events[:20]:
        print(f"  - {ev.kind}: {ev.name!r} slug={ev.linkedin_public_id!r} "
              f"bday={ev.month}/{ev.day} detail={ev.detail[:60]!r}")
    if len(events) > 20:
        print(f"  … and {len(events) - 20} more")

    if args.ingest:
        print("(use run_catch_up_ingest from app code with a DB session — probe is read-only)")


if __name__ == "__main__":
    main()
