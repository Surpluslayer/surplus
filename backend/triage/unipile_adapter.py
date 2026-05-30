"""
triage/unipile_adapter.py : a thin repair layer over the Unipile profile API.

enrich.py used to call Unipile directly and treat *any* non-200 as "not found",
silently collapsing distinct failure modes (bad params, missing slug, rate limit,
server blip) into the same dead end. That hid bugs and threw away recoverable
calls.

This adapter maps each HTTP status to the right bounded response:

    400      → parameter/schema error    → retry ONCE with minimal known-good params
    404/422  → identifier/slug not found  → no retry; caller should people-search
    429  → rate limited               → backoff + retry on a rotated account
    5xx  → transient                  → retry up to 2× with exponential backoff
    200  → success (work-exp may be empty — that's incomplete, not failed)

Retries are bounded and every attempt is logged into the result so the debug
JSON keeps the full status/error trail. Module-level counters tally outcomes
across a batch for the run summary.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

# Repeated params — Unipile wants linkedin_sections as repeated keys, not CSV.
_PROD_PARAMS: list[tuple[str, str]] = [
    ("linkedin_sections", "experience"),
    ("linkedin_sections", "about"),
]
_MINIMAL_PARAMS: list[tuple[str, str]] = []  # account_id only


# ── Counters ──────────────────────────────────────────────────────────────

class _Counters:
    """Thread-safe tally of profile-fetch outcomes for the batch summary."""
    _KEYS = (
        "unipile_profile_success",
        "unipile_profile_bad_param_repaired",
        "unipile_profile_404",
        "unipile_profile_422",
        "unipile_profile_429",
        "unipile_profile_5xx",
        "unipile_people_search_fallback",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d = {k: 0 for k in self._KEYS}

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._d[key] = self._d.get(key, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._d)

    def reset(self) -> None:
        with self._lock:
            self._d = {k: 0 for k in self._KEYS}


COUNTERS = _Counters()


# ── Result type ────────────────────────────────────────────────────────────

@dataclass
class ProfileResult:
    status: str            # success|bad_param_repaired|not_found|rate_limited|
                           # server_error|no_config|empty_slug|transport_error
    http_status: int = 0
    body: dict = field(default_factory=dict)   # parsed JSON on success
    error_body: str = ""
    params_used: list = field(default_factory=list)
    attempts: list = field(default_factory=list)  # per-attempt log

    @property
    def ok(self) -> bool:
        return self.status in ("success", "bad_param_repaired")

    @property
    def should_people_search(self) -> bool:
        """422 = identity/slug issue → caller should resolve a fresh URL."""
        return self.status == "not_found"

    def debug_dict(self) -> dict:
        return {
            "status": self.status,
            "http_status": self.http_status,
            "params_used": [list(p) for p in self.params_used],
            "error_body": self.error_body[:500],
            "attempts": self.attempts,
        }


def _param_label(params: list) -> str:
    if not params:
        return "minimal"
    return "&".join(f"{k}={v}" for k, v in params)


# ── Profile fetch with bounded repair ──────────────────────────────────────

def fetch_profile(
    dsn: str,
    api_key: str,
    account_id_fn: Callable[[], str | None],
    slug: str,
    *,
    timeout: float = 12.0,
    max_5xx_retries: int = 2,
    max_429_retries: int = 2,
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> ProfileResult:
    """Fetch a LinkedIn profile by slug, repairing known failure modes.

    account_id_fn is called per attempt so 429/5xx retries rotate accounts.
    """
    if not (dsn and api_key and slug):
        return ProfileResult(status="no_config")
    if slug.startswith("http"):
        return ProfileResult(status="empty_slug")

    url = f"{dsn}/api/v1/users/{slug}"
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    attempts: list = []

    def _get(params: list) -> tuple[int | None, httpx.Response | None, str]:
        account_id = account_id_fn()
        if not account_id:
            return None, None, "no_account_id"
        full = [("account_id", account_id)] + params
        try:
            r = httpx.get(url, headers=headers, params=full, timeout=timeout)
            return r.status_code, r, account_id
        except Exception as e:  # transport/timeout
            attempts.append({"params": _param_label(params), "account": account_id,
                             "error": str(e)[:160]})
            return None, None, account_id

    def _log(code: int | None, params: list, account: str, body: str = "") -> None:
        attempts.append({"params": _param_label(params), "account": account,
                         "status": code, "body": body[:160] if body else ""})

    def _success(r: httpx.Response, status: str, params: list) -> ProfileResult:
        COUNTERS.incr("unipile_profile_success")
        if status == "bad_param_repaired":
            COUNTERS.incr("unipile_profile_bad_param_repaired")
        try:
            body = r.json()
        except Exception:
            body = {}
        return ProfileResult(status=status, http_status=200, body=body,
                             params_used=params, attempts=attempts)

    # ── Attempt 1: production params ──────────────────────────────────────
    code, r, acct = _get(_PROD_PARAMS)
    _log(code, _PROD_PARAMS, acct, "" if (r is None or code == 200) else r.text)

    if code == 200 and r is not None:
        return _success(r, "success", _PROD_PARAMS)

    # ── 400 : parameter/schema error → repair with minimal params ─────────
    if code == 400:
        code2, r2, acct2 = _get(_MINIMAL_PARAMS)
        _log(code2, _MINIMAL_PARAMS, acct2, "" if (r2 is None or code2 == 200) else r2.text)
        if code2 == 200 and r2 is not None:
            return _success(r2, "bad_param_repaired", _MINIMAL_PARAMS)
        return ProfileResult(status="server_error" if (code2 or 0) >= 500 else "not_found"
                             if code2 == 422 else "bad_param_unrepaired",
                             http_status=code2 or 0,
                             error_body=r2.text if r2 is not None else "",
                             params_used=_MINIMAL_PARAMS, attempts=attempts)

    # ── 404 : often transient/account-visibility, not a dead slug ─────────
    # The submitted URL is the authoritative identity, so retry ONCE on a
    # rotated account before giving up. Only a persistent 404 routes to
    # people-search (which can resolve to the wrong namesake).
    if code == 404:
        code2, r2, acct2 = _get(_PROD_PARAMS)
        _log(code2, _PROD_PARAMS, acct2, "" if (r2 is None or code2 == 200) else r2.text)
        if code2 == 200 and r2 is not None:
            return _success(r2, "success", _PROD_PARAMS)
        COUNTERS.incr("unipile_profile_404")
        return ProfileResult(status="not_found", http_status=code2 or 404,
                             error_body=r2.text if r2 is not None else "",
                             params_used=_PROD_PARAMS, attempts=attempts)

    # ── 422 : malformed identifier → no retry, signal people-search ───────
    if code == 422:
        COUNTERS.incr("unipile_profile_422")
        return ProfileResult(status="not_found", http_status=422,
                             error_body=r.text if r is not None else "",
                             params_used=_PROD_PARAMS, attempts=attempts)

    # ── 429 : rate limited → backoff + rotate account ─────────────────────
    if code == 429:
        for i in range(max_429_retries):
            sleep(backoff_base * (2 ** i))
            code, r, acct = _get(_PROD_PARAMS)
            _log(code, _PROD_PARAMS, acct, "" if (r is None or code == 200) else r.text)
            if code == 200 and r is not None:
                return _success(r, "success", _PROD_PARAMS)
            if code != 429:
                break
        COUNTERS.incr("unipile_profile_429")
        return ProfileResult(status="rate_limited", http_status=429,
                             error_body=r.text if r is not None else "",
                             params_used=_PROD_PARAMS, attempts=attempts)

    # ── 5xx or transport error → retry up to N with exponential backoff ───
    if code is None or code >= 500:
        for i in range(max_5xx_retries):
            sleep(backoff_base * (2 ** i))
            code, r, acct = _get(_PROD_PARAMS)
            _log(code, _PROD_PARAMS, acct, "" if (r is None or code == 200) else r.text)
            if code == 200 and r is not None:
                return _success(r, "success", _PROD_PARAMS)
            if code is not None and code < 500 and code != 429:
                break
        COUNTERS.incr("unipile_profile_5xx")
        return ProfileResult(status="server_error" if code else "transport_error",
                             http_status=code or 0,
                             error_body=r.text if r is not None else "",
                             params_used=_PROD_PARAMS, attempts=attempts)

    # ── Any other 4xx : surface, don't pretend it's "not found" ───────────
    return ProfileResult(status="http_error", http_status=code or 0,
                         error_body=r.text if r is not None else "",
                         params_used=_PROD_PARAMS, attempts=attempts)
