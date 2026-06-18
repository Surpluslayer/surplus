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


def status() -> dict:
    """Which pieces are wired (booleans only — never leak the values)."""
    return {
        "configured": configured(),
        "api_key_set": bool(_key()),
        "profile_dataset_set": bool(_profile_dataset()),
        "posts_dataset_set": bool(_posts_dataset()),
        "webhook_url_set": bool(_webhook_url()),
        "webhook_secret_set": bool(webhook_secret()),
        "last_trigger": _LAST_TRIGGER or None,
    }


_LAST_TRIGGER: dict = {}


def last_trigger() -> dict:
    """Outcome of the last trigger attempt (for the cutover diagnostic)."""
    return _LAST_TRIGGER


def _trigger(dataset_id: str, urls: list[str], *, kind: str) -> bool:
    """Fire one async collection for `urls` against `dataset_id`, asking Bright
    Data to deliver results to our webhook. Returns True on a 2xx accept and
    records the outcome (status + response) in _LAST_TRIGGER so we can see WHY a
    run did or didn't use Bright Data. Best-effort: any failure returns False."""
    global _LAST_TRIGGER
    valid = [u for u in (urls or []) if (u or "").strip()]
    if not (dataset_id and valid and _key() and _webhook_url()):
        _LAST_TRIGGER = {"kind": kind, "ok": False, "reason": "no urls or missing config",
                         "url_count": len(valid), "dataset_set": bool(dataset_id),
                         "key_set": bool(_key()), "webhook_set": bool(_webhook_url())}
        return False
    # Route profile- vs posts-deliveries by PATH: `notify` is a boolean (job-done
    # ping), NOT a tag, so we point each dataset at /webhooks/brightdata/<kind>.
    endpoint = _webhook_url().rstrip("/") + "/" + kind
    params = {
        "dataset_id": dataset_id,
        "endpoint": endpoint,           # Bright Data POSTs results here
        "format": "json",
        "uncompressed_webhook": "true",
        "include_errors": "true",
    }
    body = [{"url": u} for u in valid]
    if kind == "posts":
        # The posts dataset DISCOVERS a profile's recent posts by profile url
        # (vs. collecting one known post URL) -- needs these discover params.
        params["type"] = "discover_new"
        params["discover_by"] = "profile_url"
        # Do NOT send start_date/end_date: many LinkedIn posts carry no
        # machine-readable date, and Bright Data FILTERS OUT every undated post
        # when a date window is set -> "Total posts: N, with dates: 0 ... dead_page"
        # (their own guidance: leave the dates empty). We get recent posts back
        # and dedup/recency-filter on our side via seen_post_ids in apply_posts.
        #
        # only_authored_posts=true: return ONLY the contact's OWN posts, not their
        # whole activity feed (reshares/likes of others). Without it, discover-by-
        # profile pulls ~hundreds of feed records per person -- noisy AND a big
        # Bright Data credit sink. With it: a contact who doesn't post = 0 records
        # (no spend); a contact who posts a milestone = just those posts. This is
        # the right signal (their news) and makes the author-side filter redundant.
        body = [{"url": u, "only_authored_posts": True} for u in valid]
    sec = webhook_secret()
    if sec:
        params["auth_header"] = f"Bearer {sec}"
    try:
        r = httpx.post(
            _TRIGGER_URL,
            params=params,
            headers={"Authorization": f"Bearer {_key()}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        ok = r.status_code < 300
        _LAST_TRIGGER = {"kind": kind, "ok": ok, "status": r.status_code,
                         "url_count": len(valid), "resp": (r.text or "")[:300]}
        return ok
    except Exception as exc:  # noqa: BLE001 : trigger is best-effort
        _LAST_TRIGGER = {"kind": kind, "ok": False, "url_count": len(valid),
                         "error": f"{type(exc).__name__}: {exc}"}
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
    """Flatten a delivered profile record to the fields the engine diffs.

    Bright Data's people-profiles schema (validated against a real delivery):
    company is `current_company_name`; the current TITLE lives inside the
    `current_company` object (.title) or the first `experience` entry — there's
    no top-level title/headline/position.
    """
    if not isinstance(record, dict):
        return {}
    cc = record.get("current_company")
    company = (record.get("current_company_name")
               or (cc.get("name") if isinstance(cc, dict) else None)
               or record.get("company") or "")
    title = (cc.get("title") if isinstance(cc, dict) else None) or ""
    if not title:
        exp = record.get("experience")
        if isinstance(exp, list) and exp and isinstance(exp[0], dict):
            title = exp[0].get("title") or exp[0].get("position") or ""
    if not title:
        title = record.get("position") or record.get("title") or ""
    return {
        "linkedin_url": record.get("url") or record.get("input_url") or record.get("linkedin_url"),
        "company": company,
        "title": title,
        "headline": record.get("headline") or title or "",
    }


def normalize_post(record: dict) -> dict:
    """Flatten ONE delivered post record (the posts dataset delivers a SEPARATE
    record per post, not one record with a nested posts[] array).

    Critically, the "discover by profile url" endpoint returns the queried
    profile's recent ACTIVITY FEED -- a mix of their own posts AND other people's
    posts they engaged with/reshared. So each record carries:
      * discovery_input.url -> the profile WE QUERIED (= the contact)        [match key]
      * use_url / user_id   -> the post's AUTHOR (often someone else)        [attribution]
      * url, post_text      -> the post itself
    The caller groups by `profile_url` and keeps only posts whose `author_url`
    matches the contact, so we never attribute someone else's raise to them.
    """
    if not isinstance(record, dict):
        return {}
    di = record.get("discovery_input")
    profile_url = di.get("url") if isinstance(di, dict) else None
    return {
        "profile_url": profile_url,
        "author_url": record.get("use_url") or "",
        "author_id": record.get("user_id") or "",
        "url": record.get("url") or record.get("id"),
        "text": (record.get("post_text") or record.get("original_post_text")
                 or record.get("title") or ""),
        "date_posted": record.get("date_posted"),
    }
