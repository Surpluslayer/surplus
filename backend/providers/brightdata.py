"""providers/brightdata.py : Bright Data Web Scraper API client (primary
updates source).

Bright Data scrapes the PUBLIC LinkedIn profile + posts for a set of URLs on
their own infrastructure (proxies/farms) and DELIVERS the structured data to our
webhook -- so the scraping, and its ban risk, never touches our or a host's
LinkedIn account. We trigger an async collection here; the results arrive at
routes/webhooks.py :: brightdata and are processed by agents/updates_engine.

Async model (Bright Data Web Scraper API):
  POST /datasets/v3/trigger?dataset_id=...&endpoint=<our webhook>&format=json
       body: [{"url": "<linkedin profile url>"}, ...]
  -> Bright Data scrapes in the background and POSTs the results to <endpoint>.

Everything is env-driven + best-effort: with no API key (or no dataset ids) we
report `configured() == False` and the engine cleanly falls back to Exa.

Required env to go live:
  BRIGHTDATA_API_KEY        - Bright Data API token
  BRIGHTDATA_PROFILE_DATASET- dataset id for the LinkedIn "people profiles" scraper
  BRIGHTDATA_POSTS_DATASET  - dataset id for the LinkedIn "posts" scraper (optional)
  BRIGHTDATA_WEBHOOK_URL    - public https URL of our /webhooks/brightdata receiver
Optional:
  BRIGHTDATA_WEBHOOK_SECRET - shared secret we verify on inbound delivery
"""
from __future__ import annotations

import os

import httpx

_TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"


def _key() -> str:
    return (os.environ.get("BRIGHTDATA_API_KEY") or "").strip()


def _profile_dataset() -> str:
    return (os.environ.get("BRIGHTDATA_PROFILE_DATASET") or "").strip()


def _posts_dataset() -> str:
    return (os.environ.get("BRIGHTDATA_POSTS_DATASET") or "").strip()


def _webhook_url() -> str:
    return (os.environ.get("BRIGHTDATA_WEBHOOK_URL") or "").strip()


def configured() -> bool:
    """True only when we can actually trigger a profile collection AND have a
    delivery target. Anything missing -> the engine falls back to Exa."""
    return bool(_key() and _profile_dataset() and _webhook_url())


def webhook_secret() -> str:
    return (os.environ.get("BRIGHTDATA_WEBHOOK_SECRET") or "").strip()


def _trigger(dataset_id: str, urls: list[str], *, kind: str) -> bool:
    """Fire one async collection for `urls` against `dataset_id`, asking Bright
    Data to deliver results to our webhook. Returns True on a 2xx accept.
    Best-effort: any failure returns False so the caller can degrade."""
    if not (dataset_id and urls and _key() and _webhook_url()):
        return False
    params = {
        "dataset_id": dataset_id,
        "endpoint": _webhook_url(),     # Bright Data POSTs results here
        "format": "json",
        "uncompressed_webhook": "true",
        # tag the delivery so the receiver knows profile-diff vs posts-cascade
        "notify": kind,
    }
    sec = webhook_secret()
    if sec:
        params["auth_header"] = f"Bearer {sec}"
    try:
        r = httpx.post(
            _TRIGGER_URL,
            params=params,
            headers={"Authorization": f"Bearer {_key()}",
                     "Content-Type": "application/json"},
            json=[{"url": u} for u in urls if (u or "").strip()],
            timeout=30,
        )
        return r.status_code < 300
    except Exception:  # noqa: BLE001 : trigger is best-effort
        return False


def trigger_updates(urls: list[str]) -> bool:
    """Trigger the profile (job-change) collection, plus the posts collection
    when that dataset is configured. Returns True if at least the profile
    collection was accepted (the signal we must have)."""
    urls = [u for u in (urls or []) if (u or "").strip()]
    if not urls:
        return False
    ok = _trigger(_profile_dataset(), urls, kind="profile")
    if _posts_dataset():
        # posts are a bonus signal; don't let their failure block the profile run
        try:
            _trigger(_posts_dataset(), urls, kind="posts")
        except Exception:  # noqa: BLE001
            pass
    return ok


# --- parsing the delivered records -----------------------------------------
# Bright Data's LinkedIn schemas vary by dataset; keep these tolerant so a
# field rename doesn't crash the receiver. (Validate exact keys against a real
# delivery once the dataset ids are set -- see updates_engine.apply_profile /
# apply_posts for which fields are consumed.)
def normalize_profile(record: dict) -> dict:
    """Flatten a delivered profile record to the fields the engine diffs."""
    if not isinstance(record, dict):
        return {}
    exp = record.get("experience") or record.get("current_company") or {}
    company = (record.get("current_company_name")
               or (exp.get("company") if isinstance(exp, dict) else None)
               or record.get("company") or "")
    return {
        "linkedin_url": record.get("url") or record.get("input_url") or record.get("linkedin_url"),
        "company": company,
        "title": (record.get("position") or record.get("title")
                  or record.get("headline") or ""),
        "headline": record.get("headline") or "",
    }


def normalize_posts(record: dict) -> dict:
    """Flatten a delivered posts record to {linkedin_url, posts:[{url,text}]}."""
    if not isinstance(record, dict):
        return {"linkedin_url": None, "posts": []}
    raw = record.get("posts") or record.get("activity") or []
    posts = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, dict):
            posts.append({"url": p.get("url") or p.get("post_url") or p.get("id"),
                          "text": p.get("text") or p.get("post_text") or p.get("title") or ""})
    return {
        "linkedin_url": record.get("url") or record.get("input_url") or record.get("linkedin_url"),
        "posts": posts,
    }
