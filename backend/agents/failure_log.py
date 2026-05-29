"""
failure_log.py : ContextVar-based collector for silent / partial-failure
reasons inside long-running pipelines (prospecting, matching, triage).

Why this exists :
  The prospecting pipeline (and matching / triage) is wired with
  fail-open semantics : a 429 from Anthropic, a timeout on Exa, a
  malformed LLM response, etc. all result in the caller silently
  returning fewer / no candidates. The operator sees "0 candidates
  surfaced" and has no idea whether their ICP is too narrow, our
  vendor 429'd, or a source crashed.

Design :
  - Module exposes a `FailureCollector` and a contextvar that holds the
    current collector (or None).
  - Pipeline orchestrators (run_prospect, run_match, ...) open a
    `collector_scope()` context, pass the collected list back to the
    route, and the route ships it to the SPA.
  - Any code anywhere in the stack can call `report_failure(...)` ; if
    a collector is active the failure is captured, otherwise it's a no-op.

ContextVars propagate across asyncio.gather() and asyncio.to_thread()
naturally (verified in Python 3.11+ docs). So we don't need to pass the
collector through every helper signature.

Kinds (taxonomy) : each is a stable string the frontend keys off when
choosing user-visible copy. Add new kinds here in one place.
"""
from __future__ import annotations
import contextvars
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Optional


# ─── Taxonomy ────────────────────────────────────────────────────────
# Stable strings. Frontend maps these to human copy in one place.
ANTHROPIC_RATE_LIMIT   = "anthropic_rate_limit"
ANTHROPIC_AUTH_ERROR   = "anthropic_auth_error"
ANTHROPIC_TIMEOUT      = "anthropic_timeout"
ANTHROPIC_ERROR        = "anthropic_error"
ANTHROPIC_PARSE_ERROR  = "anthropic_parse_error"
ANTHROPIC_NO_KEY       = "anthropic_no_key"

EXA_RATE_LIMIT         = "exa_rate_limit"
EXA_AUTH_ERROR         = "exa_auth_error"
EXA_ERROR              = "exa_error"
EXA_NO_KEY             = "exa_no_key"

SOURCE_TIMEOUT         = "source_timeout"
SOURCE_CRASH           = "source_crash"

UNIPILE_RATE_LIMIT     = "unipile_rate_limit"
UNIPILE_ERROR          = "unipile_error"

NO_MATCHES             = "no_matches"          # success-but-empty
DROPPED_NO_LINKEDIN    = "dropped_no_linkedin" # filter applied
CONFIG_MISSING         = "config_missing"


@dataclass(frozen=True)
class FailureReason:
    """One failure event captured during a pipeline run.

    kind : taxonomy string (see constants above)
    source : where it happened — adapter name, step name, etc.
             ("linkedin" / "github" / "judge" / "enrich" / ...)
    detail : short human-readable specifics ; safe to surface to the user
    """
    kind: str
    source: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FailureCollector:
    """Append-only bag of FailureReasons. Thread/asyncio safe via contextvar."""
    failures: list[FailureReason] = field(default_factory=list)

    def add(self, kind: str, source: str = "", detail: str = "") -> None:
        self.failures.append(FailureReason(kind=kind, source=source, detail=detail))

    def has(self, kind: str) -> bool:
        return any(f.kind == kind for f in self.failures)

    def as_list_of_dicts(self) -> list[dict]:
        return [f.to_dict() for f in self.failures]

    def __len__(self) -> int:
        return len(self.failures)


# Module-level contextvar : ContextVars are isolated per asyncio task /
# threadpool worker, so concurrent /prospect calls from different users
# get their own collector. Default None means "no collector active";
# report_failure() then no-ops.
_CURRENT: contextvars.ContextVar[Optional[FailureCollector]] = \
    contextvars.ContextVar("surplus_failure_collector", default=None)


@contextmanager
def collector_scope() -> "Iterator[FailureCollector]":
    """Open a collection scope. All `report_failure()` calls inside this
    scope (and any tasks spawned from it) are captured on the yielded
    collector."""
    collector = FailureCollector()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


def report_failure(kind: str, source: str = "", detail: str = "") -> None:
    """No-op when no collector is active. Otherwise appends a failure.

    Safe to call from anywhere — agent code, adapters, LLM helpers,
    even nested asyncio tasks. The contextvar's task-aware semantics
    make sure this lands on the right collector for the right user
    request.
    """
    bag = _CURRENT.get()
    if bag is None:
        return
    bag.add(kind=kind, source=source, detail=str(detail)[:240])


def classify_anthropic_exception(exc: Exception) -> str:
    """Best-effort mapping from an Anthropic SDK exception class name to
    our taxonomy. Doesn't import the SDK so this module stays cheap to
    load in environments without anthropic installed."""
    name = type(exc).__name__
    if "RateLimit" in name:
        return ANTHROPIC_RATE_LIMIT
    if "Authentication" in name or "Permission" in name:
        return ANTHROPIC_AUTH_ERROR
    if "Timeout" in name:
        return ANTHROPIC_TIMEOUT
    if "BadRequest" in name or "Validation" in name:
        return ANTHROPIC_ERROR
    return ANTHROPIC_ERROR


def classify_http_status(status_code: int, provider: str) -> str:
    """Map an HTTP status code from an external provider into our taxonomy."""
    if provider == "exa":
        if status_code == 429:
            return EXA_RATE_LIMIT
        if status_code in (401, 403):
            return EXA_AUTH_ERROR
        return EXA_ERROR
    if provider == "anthropic":
        if status_code == 429:
            return ANTHROPIC_RATE_LIMIT
        if status_code in (401, 403):
            return ANTHROPIC_AUTH_ERROR
        return ANTHROPIC_ERROR
    if provider == "unipile":
        if status_code == 429:
            return UNIPILE_RATE_LIMIT
        return UNIPILE_ERROR
    return f"{provider}_error"
