"""metrics.py : tiny in-process metrics so you can monitor health from ONE URL
instead of scrolling Railway logs.

The request-log middleware feeds request outcomes here; the book LLM helper feeds
Claude-call outcomes here; the rate-gate reports its live in-flight state. An
admin endpoint (/api/book/_diagnostics) returns a snapshot: counts, recent errors /
slow requests, per-route latency, and whether the relationship layer is
throttling right now.

Caveat: in-memory and PER-REPLICA. Prod runs ~2 replicas, so a snapshot reflects
the one replica that answered -- hit it a couple times for a fuller picture. Good
enough to answer "is it erroring / slow / throttling" without log-diving; a real
multi-replica view needs an external collector (Sentry / Better Stack) later.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_BOOT = time.time()

# Rolling counters.
_req = defaultdict(int)          # total, 2xx, 3xx, 4xx, 5xx, slow
_llm = defaultdict(int)          # ok, err
# Recent notable events (errors + slow), newest last.
_recent = deque(maxlen=60)       # {ts, kind, method, path, status, ms, detail}
# Per-route recent latencies for p50/p95.
_route_ms = defaultdict(lambda: deque(maxlen=120))
_llm_ms = deque(maxlen=200)      # recent Claude-call durations (ms)


def record_request(method: str, path: str, status: int, ms: float) -> None:
    with _lock:
        _req["total"] += 1
        _req[f"{(status // 100) if status else 0}xx"] += 1
        if ms >= 5000:
            _req["slow"] += 1
        _route_ms[f"{method} {path}"].append(ms)
        if status >= 400 or ms >= 5000:
            _recent.append({"ts": time.time(), "kind": "req", "method": method,
                            "path": path, "status": status, "ms": round(ms)})


def record_llm(label: str, ms: float, ok: bool, detail: str = "") -> None:
    with _lock:
        _llm["ok" if ok else "err"] += 1
        _llm_ms.append(ms)
        if not ok:
            _recent.append({"ts": time.time(), "kind": "llm", "label": label,
                            "ms": round(ms), "detail": detail})


def _pct(samples, q):
    if not samples:
        return 0
    s = sorted(samples)
    i = min(len(s) - 1, int(len(s) * q))
    return round(s[i])


def _diagnose(recent: list, gate: dict, llm: dict) -> str:
    """Turn the numbers into one plain-English sentence: what's wrong, or healthy.
    So you read an answer, not a blob -- no interpreting raw metrics."""
    saturated = bool(gate) and gate.get("in_flight", 0) >= gate.get("total", 1) \
        and gate.get("fg_waiting", 0) > 0
    # newest-first
    slow = [e for e in recent if e.get("kind") == "req" and (e.get("ms") or 0) >= 5000]
    errs = [e for e in recent if e.get("kind") == "req" and (e.get("status") or 0) >= 500]
    llm_err = [e for e in recent if e.get("kind") == "llm"]
    if saturated:
        return (f"THROTTLING NOW: Claude gate saturated "
                f"({gate['in_flight']}/{gate['total']} in flight, "
                f"{gate['fg_waiting']} user call(s) waiting). Fan-out is starving "
                f"live requests -- the relationship-layer throttle.")
    if slow:
        e = slow[0]
        return (f"SLOW REQUEST {e['age_s']}s ago: {e['method']} {e['path']} took "
                f"{e['ms']/1000:.0f}s -- likely a Claude fan-out / rate-limit backoff "
                f"(this is what shows the user 'server took too long').")
    if errs:
        e = errs[0]
        return (f"SERVER ERROR {e['age_s']}s ago: {e['method']} {e['path']} -> "
                f"{e['status']}. Check the [req] traceback in logs for the exception.")
    if llm_err:
        e = llm_err[0]
        return (f"CLAUDE CALL FAILED {e['age_s']}s ago [{e.get('label','')}]: "
                f"{e.get('detail','')} -- usually a 429 (rate limit) or bad JSON.")
    return "Healthy -- no errors, slow requests, or throttling recently."


def snapshot() -> dict:
    # Pull the gate's live state without a hard import dependency.
    try:
        from .agents import rategate
        gate = rategate.stats()
    except Exception:  # noqa: BLE001
        gate = {}
    with _lock:
        now = time.time()
        routes = {}
        for r, d in _route_ms.items():
            if d:
                routes[r] = {"n": len(d), "p50": _pct(d, 0.50),
                             "p95": _pct(d, 0.95), "max": round(max(d))}
        recent = [{**e, "age_s": round(now - e["ts"], 1)} for e in _recent]
        recent_newest = list(reversed(recent))
        llm = {**dict(_llm), "p50_ms": _pct(_llm_ms, 0.50),
               "p95_ms": _pct(_llm_ms, 0.95)}
        return {
            "summary": _diagnose(recent_newest, gate, llm),
            "uptime_s": round(now - _BOOT),
            "requests": dict(_req),
            "llm": llm,
            "gate": gate,
            "recent": recent_newest,  # newest first
        }
