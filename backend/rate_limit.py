"""
rate_limit.py : per-IP sliding-window rate limiter for anonymous,
expensive, or user-creating endpoints.

Why in-memory : surplus runs 2 replicas behind Railway's LB. Memory
state per-replica means an attacker hitting one replica with N RPS
can still get ~2N RPS overall by chance-hitting both. That's
acceptable at our scale ; it would NOT be acceptable for a real
rate-limit story (Redis / Postgres counter). Our goal here is
defense-in-depth before Tech Week, not perfection.

Cloudflare WAF rules can be layered on top later for the proper fix.

Usage :
    from backend.rate_limit import per_ip_rate_limit
    @router.post("/api/auth/triage/quick-start",
                 dependencies=[Depends(per_ip_rate_limit(limit=5, window_s=60))])

When the limit is exceeded the dependency raises HTTPException 429
with a `Retry-After` header so well-behaved clients back off.

The IP is read from `X-Forwarded-For` (Railway / Cloudflare set this)
with a fallback to the connecting peer. CF / Railway terminate TLS
upstream so the direct client IP at uvicorn would be the LB's IP ;
we MUST read the forwarded header to get the real caller.
"""
from __future__ import annotations
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request, status


# Per-key sliding window of timestamps. key = "<route_tag>:<client_ip>".
# defaultdict(deque) so a new caller's bucket lazily appears.
_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Best-effort real-client IP.

    Order (most-trusted first) :
      1. CF-Connecting-IP : Cloudflare's own header. They strip it from
         inbound requests, so if we see it, it's from CF.
      2. X-Forwarded-For : Railway sets this. Takes the LEFTMOST entry
         since each proxy appends.
      3. request.client.host : last resort, will be the LB's IP in prod.
    """
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",", 1)[0].strip()
    return (request.client.host if request.client else "unknown")


def per_ip_rate_limit(
    limit: int,
    window_s: int,
    tag: str = "default",
) -> Callable:
    """Build a FastAPI dependency that allows `limit` requests per
    `window_s` seconds per (tag, IP). Different tags get independent
    windows : e.g. signup vs checkout share an IP but each gets its
    own quota.
    """
    def dep(request: Request) -> None:
        now = time.monotonic()
        ip = _client_ip(request)
        key = f"{tag}:{ip}"
        bucket = _WINDOWS[key]
        # Trim entries outside the window.
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            # Compute the wait time : when does the oldest entry age out?
            wait = max(1, int(bucket[0] + window_s - now) + 1)
            print(f"  [rate_limit] {key} blocked : {len(bucket)} req in "
                  f"{window_s}s window (retry in {wait}s)")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": (
                        f"Too many requests. Try again in ~{wait} seconds."
                    ),
                },
                headers={"Retry-After": str(wait)},
            )
        bucket.append(now)
    return dep
