"""Brief-keyed cache for expensive LLM calls.

Saves serialized JSON results under .cache/<key>.json keyed by a hash of
(prompt content, model, version). Re-running the same brief is free — no
LLM call, no web search.

Cache is intentionally local-only (gitignored) and easy to clear:
    rm -rf .cache/
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


# Cache path is configurable via env so it can live somewhere writable on
# Railway (the container working dir varies by plan). Defaults to /tmp on
# any container-like environment, else ./.cache for local dev.
CACHE_DIR = Path(os.environ.get(
    "MATCH_CACHE_DIR",
    "/tmp/surplus-match-cache" if os.path.isdir("/app") else ".cache",
))


def _enabled() -> bool:
    """Cache on by default; opt out with EI_DISABLE_CACHE=1."""
    return os.environ.get("EI_DISABLE_CACHE", "").lower() not in {"1", "true", "yes"}


def _key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def get(namespace: str, *parts: str) -> Any | None:
    if not _enabled():
        return None
    key = _key(namespace, *parts)
    path = CACHE_DIR / namespace / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def put(namespace: str, value: Any, *parts: str) -> None:
    if not _enabled():
        return
    key = _key(namespace, *parts)
    path = CACHE_DIR / namespace / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str))
