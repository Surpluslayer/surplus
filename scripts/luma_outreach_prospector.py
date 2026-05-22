#!/usr/bin/env python3
"""scripts/luma_outreach_prospector.py

Crawl Luma events, find organizer contact emails, classify the event type
with Claude, draft a personalized surplus pitch, and optionally send via SMTP.

Designed as a GTM prospecting tool for surplus : feeds the same triage product
the operator runs for sponsors back at event organizers themselves.

Usage:
  # From a curated URL list (one lu.ma URL per line, blanks + # comments ok)
  python -m scripts.luma_outreach_prospector --urls-file urls.txt --out out.csv

  # Discover from a city / discover seed page (best-effort scrape)
  python -m scripts.luma_outreach_prospector \\
      --seed-page https://lu.ma/sf --max 25 --out out.csv

  # Both inputs combined; cap concurrent fetches
  python -m scripts.luma_outreach_prospector \\
      --urls-file urls.txt --seed-page https://lu.ma/nyc --max 10

  # Live send (off by default; caps at --daily-cap per run)
  python -m scripts.luma_outreach_prospector \\
      --urls-file urls.txt --send --daily-cap 20

Env:
  ANTHROPIC_API_KEY  required for event classification + pitch drafting
  EXA_API_KEY        optional; enables host-name email search fallback
  SMTP_HOST/PORT     required with --send (PORT defaults to 587)
  SMTP_USER/PASS     required with --send
  SMTP_FROM          required with --send (e.g. "Surplus <hi@usesurplus.com>")
  SURPLUS_SENDER_NAME / SURPLUS_SENDER_ROLE  shown in the email signature
  SURPLUS_PHYSICAL_ADDRESS                   CAN-SPAM requirement when --send

Output CSV columns:
  url, name, host_name, contact_email, email_source, event_type, audience,
  pitch_paragraph, subject, body, sent, send_error
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# Make the backend package importable when running this as a top-level script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx

from backend.jsonx import extract_json
from backend.triage.luma import (
    LUMA_HOSTS,
    LumaEvent,
    LumaFetchError,
    fetch_luma_event,
)


# ── URL collection ─────────────────────────────────────────────────────

# A Luma event slug : letters, digits, hyphens. Excludes obvious city / nav
# slugs we don't want to treat as events (sf, nyc, discover, signin, etc.).
_EVENT_HREF_RE = re.compile(
    r'href=["\'](?:https?://(?:www\.)?(?:lu\.ma|luma\.com))?(/[a-z0-9][a-z0-9-]{2,})["\']',
    re.IGNORECASE,
)
_NON_EVENT_SLUGS = {
    "sf", "sfo", "nyc", "la", "lax", "chicago", "chi", "boston", "bos",
    "seattle", "sea", "austin", "atx", "miami", "mia", "london", "paris",
    "berlin", "tokyo", "singapore", "sg", "toronto", "yyz",
    "discover", "signin", "signup", "home", "about", "pricing", "help",
    "support", "terms", "privacy", "blog", "create", "explore",
    "calendar", "events", "settings", "account", "login", "logout",
    "u", "user", "users", "p", "embed", "api", "static", "assets",
}


def discover_event_urls(seed_html: str, seed_url: str) -> list[str]:
    """Best-effort extract lu.ma event URLs from a seed page's HTML.

    Luma's city / discover pages SSR a partial event list inline; the rest
    loads via JS. We pick up what's in the initial HTML and dedupe. Returns
    [] when nothing parseable is present so callers degrade gracefully."""
    found: list[str] = []
    seen: set[str] = set()
    base = seed_url
    for m in _EVENT_HREF_RE.finditer(seed_html):
        path = m.group(1)
        slug = path.lstrip("/").split("/", 1)[0].lower()
        if not slug or slug in _NON_EVENT_SLUGS:
            continue
        if len(slug) < 4:
            # Real event slugs are typically 6+ chars; very short slugs
            # are usually nav / city aliases we want to skip.
            continue
        full = urljoin(base, path)
        host = (urlparse(full).hostname or "").lower()
        if host not in LUMA_HOSTS:
            continue
        if full in seen:
            continue
        seen.add(full)
        found.append(full)
    return found


def fetch_seed_page(seed_url: str, *, timeout: float = 10.0) -> str:
    """GET the seed page's HTML with a browser-ish UA. Raises on non-2xx."""
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0 Safari/537.36"
        ),
        "accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(seed_url, headers=headers)
        resp.raise_for_status()
        return resp.text


def load_urls_file(path: Path) -> list[str]:
    """One URL per line; blanks and # comments ignored."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ── Email finding ──────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Emails that are noise : platform / generic / unrelated.
_EMAIL_BLOCKLIST_DOMAINS = {
    "luma.com", "lu.ma", "sentry.io", "wixpress.com", "example.com",
    "sentry-next.wixpress.com",
}
_EMAIL_BLOCKLIST_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "support",
    "help", "info",  # too generic to be a useful contact for cold outreach
}


def _is_usable_email(addr: str) -> bool:
    """Filter out platform / generic / obvious noise emails."""
    addr = addr.lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    if domain in _EMAIL_BLOCKLIST_DOMAINS:
        return False
    if local in _EMAIL_BLOCKLIST_LOCAL:
        return False
    # Strip image / asset filenames that happen to match the regex
    # (e.g. "logo@2x.png" wouldn't match, but ".jpg@cdn" sometimes does).
    if any(domain.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg")):
        return False
    return True


def find_email_in_text(text: str) -> Optional[str]:
    """Return the first usable email mentioned in `text`, or None."""
    if not text:
        return None
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(0)
        if _is_usable_email(addr):
            return addr
    return None


def find_email_via_exa(host_name: str, event_name: str) -> Optional[str]:
    """Best-effort : search Exa for the host's public web presence and try
    to pull a contact email out of the returned page text. Returns None
    when EXA_API_KEY isn't set, when no candidate page text is returned,
    or when no usable email surfaces."""
    if not host_name:
        return None
    api_key = (os.environ.get("EXA_API_KEY") or "").strip()
    if not api_key:
        return None
    query = f"{host_name} event organizer contact email"
    if event_name:
        query = f"{host_name} {event_name} organizer contact email"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://api.exa.ai/search",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={
                    "query": query,
                    "type": "neural",
                    "numResults": 5,
                    "contents": {"text": True},
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [exa.find_email] {host_name}: "
              f"{type(exc).__name__}: {exc}")
        return None
    if resp.status_code >= 400:
        print(f"  [exa.find_email] {host_name}: HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    for result in data.get("results", []):
        text = (result.get("text") or "")
        if not text:
            continue
        hit = find_email_in_text(text)
        if hit:
            return hit
    return None


# ── Event classification + pitch drafting ──────────────────────────────

_PITCH_MODEL = "claude-haiku-4-5-20251001"
_PITCH_MAX_TOKENS = 1200
_PITCH_TIMEOUT_S = 30

_PITCH_SYSTEM = """You are helping a B2B product called "surplus" reach out to \
event organizers on Luma. Surplus is an applicant-triage product : organizers \
upload a CSV of registrants, surplus scores fit against the event's stated \
goal, and returns an accept / maybe / reject recommendation per applicant \
with reasoning. It saves organizers hours of manual review and helps them \
keep the room high-signal.

You read a public Luma event description and return JSON only. Be concrete \
and grounded in what the description actually says. If the description is \
vague, say so in the pitch rather than fabricating specifics.

Schema:
{
  "event_type": string,
        // e.g. "AI hackathon", "founder dinner", "VC office hours",
        //      "hiring mixer", "community meetup", "panel + networking"
  "audience": string,
        // 1 sentence describing who the event is for, in concrete terms.
  "pain_point": string,
        // 1 sentence : the specific reason THIS event would benefit from
        // applicant triage (e.g. "limited seats vs. likely oversubscription",
        // "sponsor wants founders only", "invite-only vibes to defend").
  "pitch_paragraph": string,
        // 2-4 sentences. Personalized, references something specific from
        // the event description. Does NOT use the words "I hope this finds
        // you well" or other cold-email cliches. No emojis.
  "subject_line": string
        // <= 60 chars. References the event by name or theme. No clickbait.
}"""


def classify_and_pitch(event: LumaEvent) -> dict:
    """Call Haiku once : classify the event and draft a personalized pitch.

    Returns {} on any failure so the caller can still emit a row with empty
    pitch fields and the operator can fill them in manually."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return {}
    if not (event.name or event.description):
        return {}

    parts = []
    if event.name:
        parts.append(f"Event title: {event.name}")
    if event.host_name:
        parts.append(f"Host: {event.host_name}")
    if event.location:
        parts.append(f"Location: {event.location}")
    if event.capacity:
        parts.append(f"Capacity: {event.capacity}")
    parts.append("")
    parts.append("Description:")
    parts.append(event.description or "(no description provided)")
    user_msg = "\n".join(parts)

    try:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=_PITCH_MODEL,
            max_tokens=_PITCH_MAX_TOKENS,
            timeout=_PITCH_TIMEOUT_S,
            system=_PITCH_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [pitch] {event.url}: {type(exc).__name__}: {exc}")
        return {}

    text_chunks = [b.text for b in resp.content
                   if getattr(b, "type", "") == "text"]
    parsed = extract_json("\n".join(text_chunks)) or {}
    # Normalize : every field is a string, missing → "".
    return {
        "event_type": str(parsed.get("event_type") or "").strip(),
        "audience": str(parsed.get("audience") or "").strip(),
        "pain_point": str(parsed.get("pain_point") or "").strip(),
        "pitch_paragraph": str(parsed.get("pitch_paragraph") or "").strip(),
        "subject_line": str(parsed.get("subject_line") or "").strip(),
    }


def compose_email_body(event: LumaEvent, pitch: dict) -> str:
    """Assemble the full email body : pitch paragraph + how-it-works + sig +
    CAN-SPAM unsubscribe footer."""
    sender_name = (os.environ.get("SURPLUS_SENDER_NAME") or "the surplus team").strip()
    sender_role = (os.environ.get("SURPLUS_SENDER_ROLE") or "").strip()
    physical_addr = (os.environ.get("SURPLUS_PHYSICAL_ADDRESS") or "").strip()

    greeting_name = (event.host_name or "there").strip()
    pitch_para = pitch.get("pitch_paragraph") or (
        f"Saw {event.name or 'your upcoming event'} on Luma and wanted to "
        f"reach out. We built surplus to help event hosts triage registrants "
        f"against their goal so the room stays high-signal."
    )

    lines = [
        f"Hi {greeting_name},",
        "",
        pitch_para,
        "",
        "How it works in 60 seconds:",
        "  1. You paste your Luma registrant CSV into surplus.",
        "  2. We score each applicant against the event's goal + ideal-attendee profile.",
        "  3. You get an accept / maybe / reject list with per-applicant reasoning.",
        "",
        "Happy to run your next event's list through it as a free demo. Reply "
        "with the Luma URL and I'll send back the ranked CSV in under an hour.",
        "",
        f"— {sender_name}" + (f", {sender_role}" if sender_role else ""),
        "  surplus  ·  https://usesurplus.com",
    ]
    if physical_addr:
        lines += ["", physical_addr]
    lines += [
        "",
        "---",
        "You're getting this because you're hosting a public event on Luma. "
        "Reply STOP and I won't email you again.",
    ]
    return "\n".join(lines)


# ── SMTP send ──────────────────────────────────────────────────────────


class SMTPConfigError(RuntimeError):
    pass


def _smtp_config() -> dict:
    missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")
               if not (os.environ.get(k) or "").strip()]
    if missing:
        raise SMTPConfigError(
            "missing SMTP env vars : " + ", ".join(missing)
        )
    if not (os.environ.get("SURPLUS_PHYSICAL_ADDRESS") or "").strip():
        # CAN-SPAM requires a valid postal address in commercial email.
        raise SMTPConfigError(
            "SURPLUS_PHYSICAL_ADDRESS is required when --send is set "
            "(CAN-SPAM compliance)"
        )
    return {
        "host": os.environ["SMTP_HOST"].strip(),
        "port": int((os.environ.get("SMTP_PORT") or "587").strip()),
        "user": os.environ["SMTP_USER"].strip(),
        "password": os.environ["SMTP_PASS"].strip(),
        "from_addr": os.environ["SMTP_FROM"].strip(),
    }


def send_via_smtp(
    to_addr: str, subject: str, body: str, cfg: dict,
) -> tuple[bool, str]:
    """Send a single message. Returns (sent_ok, error_message)."""
    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ── Send log : dedupe + daily cap ──────────────────────────────────────


def _load_send_log(path: Path) -> dict:
    if not path.exists():
        return {"sent_emails": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"sent_emails": []}


def _save_send_log(path: Path, log: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2), encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    if args.urls_file:
        for u in load_urls_file(Path(args.urls_file)):
            if u not in seen:
                seen.add(u)
                urls.append(u)
        print(f"  loaded {len(urls)} URL(s) from {args.urls_file}")
    if args.seed_page:
        try:
            html = fetch_seed_page(args.seed_page)
        except Exception as exc:  # noqa: BLE001
            print(f"  seed-page fetch failed: {type(exc).__name__}: {exc}")
            html = ""
        discovered = discover_event_urls(html, args.seed_page)
        added = 0
        for u in discovered:
            if u not in seen:
                seen.add(u)
                urls.append(u)
                added += 1
                if args.max and added >= args.max:
                    break
        print(f"  discovered {added} URL(s) from seed {args.seed_page}"
              + ("" if added else " — Luma likely client-renders the list;"
                 " supply --urls-file instead"))
    return urls


def process_one(url: str) -> dict:
    """Fetch one Luma event → classify → find email → assemble row."""
    row: dict = {
        "url": url,
        "name": "",
        "host_name": "",
        "contact_email": "",
        "email_source": "",
        "event_type": "",
        "audience": "",
        "pain_point": "",
        "pitch_paragraph": "",
        "subject": "",
        "body": "",
        "sent": "",
        "send_error": "",
        "error": "",
    }
    try:
        ev = fetch_luma_event(url)
    except LumaFetchError as exc:
        row["error"] = str(exc)
        return row
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row

    row["name"] = ev.name or ""
    row["host_name"] = ev.host_name or ""

    email = find_email_in_text(ev.description or "")
    if email:
        row["contact_email"] = email
        row["email_source"] = "description"
    else:
        email = find_email_via_exa(ev.host_name or "", ev.name or "")
        if email:
            row["contact_email"] = email
            row["email_source"] = "exa"

    pitch = classify_and_pitch(ev)
    row["event_type"] = pitch.get("event_type", "")
    row["audience"] = pitch.get("audience", "")
    row["pain_point"] = pitch.get("pain_point", "")
    row["pitch_paragraph"] = pitch.get("pitch_paragraph", "")

    subject = pitch.get("subject_line") or (
        f"quick idea for {ev.name}" if ev.name else "quick idea for your next event"
    )
    row["subject"] = subject[:120]
    row["body"] = compose_email_body(ev, pitch)
    return row


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Luma events, draft per-organizer surplus pitches.",
    )
    parser.add_argument("--urls-file",
                        help="Path to file with one lu.ma URL per line")
    parser.add_argument("--seed-page",
                        help="A lu.ma/<city> or lu.ma/discover URL to scrape "
                             "for event links (best-effort)")
    parser.add_argument("--max", type=int, default=0,
                        help="Cap how many seed-page discoveries to use")
    parser.add_argument("--out", default="out/luma_prospects.csv",
                        help="Output CSV path (default: out/luma_prospects.csv)")
    parser.add_argument("--send", action="store_true",
                        help="Actually send via SMTP. Requires full SMTP env "
                             "+ SURPLUS_PHYSICAL_ADDRESS (CAN-SPAM).")
    parser.add_argument("--daily-cap", type=int, default=20,
                        help="Max messages to send in one run (default: 20)")
    parser.add_argument("--send-log", default="out/.luma_outreach_send_log.json",
                        help="Where to remember who's already been emailed")
    parser.add_argument("--delay-seconds", type=float, default=2.0,
                        help="Pause between event fetches to be polite")
    args = parser.parse_args(argv)

    if not (args.urls_file or args.seed_page):
        parser.error("supply at least one of --urls-file / --seed-page")

    urls = collect_urls(args)
    if not urls:
        print("no URLs to process; exiting.")
        return 1

    smtp_cfg = None
    if args.send:
        try:
            smtp_cfg = _smtp_config()
        except SMTPConfigError as exc:
            print(f"--send refused : {exc}")
            return 2

    send_log_path = Path(args.send_log)
    send_log = _load_send_log(send_log_path)
    already_sent_to: set[str] = set(send_log.get("sent_emails", []))
    sent_this_run = 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "url", "name", "host_name", "contact_email", "email_source",
        "event_type", "audience", "pain_point", "pitch_paragraph",
        "subject", "body", "sent", "send_error", "error",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for idx, url in enumerate(urls, 1):
            print(f"[{idx}/{len(urls)}] {url}")
            row = process_one(url)
            if row["error"]:
                print(f"    skip : {row['error']}")
            else:
                summary = row["event_type"] or "(unclassified)"
                contact = row["contact_email"] or "no public email"
                print(f"    {summary} | {contact}")

            should_send = (
                args.send
                and smtp_cfg is not None
                and row["contact_email"]
                and not row["error"]
                and row["contact_email"] not in already_sent_to
                and sent_this_run < args.daily_cap
            )
            if should_send:
                ok, err = send_via_smtp(
                    row["contact_email"], row["subject"], row["body"], smtp_cfg,
                )
                if ok:
                    row["sent"] = "yes"
                    already_sent_to.add(row["contact_email"])
                    send_log.setdefault("sent_emails", []).append(row["contact_email"])
                    _save_send_log(send_log_path, send_log)
                    sent_this_run += 1
                    print(f"    sent → {row['contact_email']} "
                          f"({sent_this_run}/{args.daily_cap})")
                else:
                    row["sent"] = "no"
                    row["send_error"] = err
                    print(f"    send FAILED : {err}")
            elif args.send and row["contact_email"] in already_sent_to:
                row["sent"] = "skipped:already-sent"
            elif args.send and sent_this_run >= args.daily_cap:
                row["sent"] = "skipped:daily-cap"
            elif args.send and not row["contact_email"]:
                row["sent"] = "skipped:no-email"

            writer.writerow(row)
            fh.flush()
            if idx < len(urls) and args.delay_seconds > 0:
                time.sleep(args.delay_seconds)

    print(f"\nwrote {out_path}")
    if args.send:
        print(f"sent {sent_this_run} message(s) this run "
              f"(cap {args.daily_cap}, total ever: {len(already_sent_to)})")
    else:
        print("dry run : no emails sent. review the CSV, then re-run with --send.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
