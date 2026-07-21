"""reqlog.py : one-line-per-request visibility so a user-facing error always has
a logged cause.

Most "the server errored" reports are NOT a 500 -- they're a client/edge timeout
on a slow request (the server returns 200 late, the browser already gave up). So
the existing logs show `200 OK` and the real cause is invisible. This middleware
logs every non-noise request's status + duration, flags slow ones as `>>> SLOW`,
and logs a traceback for any unhandled exception (the real 500s). One grep
(`[req]`) then explains every error.

Pure-ASGI (not BaseHTTPMiddleware) so it never buffers the body -- safe for
streaming responses.
"""
from __future__ import annotations

import time
import traceback

# Don't log the firehose: health probes, static assets, robots.
_SKIP_EXACT = {"/robots.txt", "/favicon.ico"}
_SKIP_PREFIX = ("/assets/",)
_SKIP_SUFFIX = ("/health",)
_SLOW_MS = 5000  # flag anything past this as the likely cause of a client timeout


def _skip(path: str) -> bool:
    return (path in _SKIP_EXACT
            or path.startswith(_SKIP_PREFIX)
            or path.endswith(_SKIP_SUFFIX))


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "")
        t0 = time.monotonic()
        status = {"code": 0}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:  # noqa: BLE001 : log the real 500 cause, then re-raise
            dur = (time.monotonic() - t0) * 1000
            print(f"[req] {method} {path} -> 500 EXC in {dur:.0f}ms: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            raise
        dur = (time.monotonic() - t0) * 1000
        if _skip(path):
            return
        code = status["code"]
        # Feed the in-process metrics store (monitor via /api/book/_diagnostics without
        # log-diving). Best-effort: never let metrics break a response.
        try:
            from . import metrics
            metrics.record_request(method, path, code, dur)
        except Exception:  # noqa: BLE001
            pass
        # Quiet on the happy path; loud on anything that explains a user error.
        if code >= 400 or dur >= _SLOW_MS:
            tag = ">>> SLOW " if dur >= _SLOW_MS else ""
            print(f"[req] {tag}{method} {path} -> {code} in {dur:.0f}ms", flush=True)
