"""triage/enrichment_cache.py : cross-event, identity-keyed enrichment cache.

WHY THIS EXISTS
---------------
Every live LinkedIn (Unipile) profile read is a real account action against a
tiny pool of team accounts — the binding constraint behind the "stripped-200"
soft-throttle. ``Applicant.enrichment_raw`` already freezes evidence so a re-run
of ONE event is free, but it is keyed on the applicant *row*: the same person
applying to another event is a new row and gets re-fetched, re-burning an action.

This module caches the frozen ``RawEvidence`` by *resolved identity*, shared
across events, so a person enriched once is reused everywhere. It is the first
concrete layer of the migration documented in ``enrichment_provider.py`` (the
read-through cache). It does NOT change the evidence shape — downstream
reconcile/score/consolidate are untouched.

DESIGN
  - STRONG KEYS ONLY. A write/read key is a LinkedIn slug ("li:<slug>") or a
    salted email hash ("em:<sha256>"). Never a name — a name-only key collides
    across people (the Brittany/Kyndred class of bug). ``identity_keys`` returns
    every strong key derivable from the inputs; the same profile is written under
    each, so a future event that knows EITHER the email OR the LinkedIn URL hits.
  - EMAIL-FIRST IS THE POINT. The email hash is free on every Luma row; the slug
    costs a people-search to obtain. So an email hit avoids the search action too,
    not just the profile fetch.
  - OWN SESSION. evaluate_all holds one Session on the event-loop thread and
    commits once at the end. The cache is independent of that transaction (a
    cached profile is valid regardless of whether the event's evaluation
    commits), so cache_get/cache_put open their OWN short-lived SessionLocal and
    commit independently. Callers invoke them via asyncio.to_thread so the shared
    session stays single-threaded and DB I/O never blocks the loop.
  - FAIL-SOFT. Any DB/JSON error degrades to a cache miss (cache_get -> None) or a
    silent skip (cache_put), never an exception. A broken cache must never sink a
    triage run — same contract as enrichment never raising.
  - QUALITY GUARD. We never cache an empty pull or a throttle-stripped one
    (work_unreliable) — that mirrors the enrichment_raw freeze guard, so a gutted
    profile can't ossify across events.
  - FLAG-GATED. Off unless TRIAGE_ENRICH_CACHE is truthy. When off, every helper
    is a no-op and behavior is exactly today's.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from .enrich import RawEvidence, _linkedin_slug
from .unipile_adapter import COUNTERS

# Stable per-deploy salt for the email hash. It only needs to be deterministic
# across runs (so the same email maps to the same key); a fixed default is fine,
# override via env if you want to rotate.
_SALT = (os.environ.get("TRIAGE_CACHE_SALT") or "surplus-triage-enrich-v1").strip()

_DEFAULT_TTL_DAYS = 30


def cache_enabled() -> bool:
    """True only when TRIAGE_ENRICH_CACHE is explicitly turned on."""
    return (os.environ.get("TRIAGE_ENRICH_CACHE") or "").strip().lower() in (
        "1", "true", "yes", "on", "y")


def _ttl_days() -> int:
    """Max age (days) a cached entry stays fresh. Job title / company change a
    few times a career, so 30 days is a safe default; tune via env."""
    try:
        return max(1, int((os.environ.get("TRIAGE_ENRICH_CACHE_TTL_DAYS") or "").strip()))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_DAYS


def _email_hash(email: str) -> str:
    """Salted sha256 of a lowercased real email, truncated. '' for blank/free
    addresses we can't key on confidently.

    We require a real (non-free) mailbox domain — a gmail/outlook address is a
    fine *key* on its own (the full address is unique to a person), so unlike the
    company-domain logic we DO key on free-provider emails here; we only drop
    addresses with no '@' at all."""
    e = (email or "").strip().lower()
    if not e or "@" not in e or e.startswith("@") or e.endswith("@"):
        return ""
    return hashlib.sha256((_SALT + "|" + e).encode("utf-8")).hexdigest()[:40]


def _phone_hash(phone: str) -> str:
    """Salted sha256 of a normalized phone number. We key on the LAST 10 digits so
    a country-code'd form (+1 415 555 1234) and a national one (415-555-1234) match
    for the common case. '' when we can't key confidently (< 10 digits). The '|ph|'
    namespace keeps phone hashes from ever colliding with email hashes."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return hashlib.sha256((_SALT + "|ph|" + digits[-10:]).encode("utf-8")).hexdigest()[:40]


def identity_keys(*, email: str = "", linkedin_url: str = "",
                  phone: str = "") -> list[str]:
    """Every STRONG cache key derivable from these inputs, strongest first
    (li > em > ph). Returns [] when none is available (we never key on a weak
    signal). Order matters: callers read in this order and stop at the first fresh
    hit, so the strongest identity wins. `phone` powers WhatsApp/SMS contacts, which
    have no email/LinkedIn to key on."""
    keys: list[str] = []
    slug = _linkedin_slug(linkedin_url) if linkedin_url else ""
    if slug:
        keys.append("li:" + slug.strip().lower())
    eh = _email_hash(email)
    if eh:
        keys.append("em:" + eh)
    ph = _phone_hash(phone)
    if ph:
        keys.append("ph:" + ph)
    return keys


def _is_fresh(fetched_at: Optional[datetime]) -> bool:
    """True if the entry is within the TTL. Defensive about naive vs tz-aware
    timestamps (SQLite stores naive; Postgres tz-aware) so a naive row from
    SQLite doesn't raise on subtraction."""
    if fetched_at is None:
        return False
    now = datetime.now(timezone.utc)
    ts = fetched_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = now - ts
    return age.total_seconds() <= _ttl_days() * 86400


def cache_get(keys: list[str]) -> Optional[RawEvidence]:
    """Return cached RawEvidence for the first fresh key, or None on miss.

    Pure read; opens + closes its own session. Fail-soft: any error -> None.
    Run this via asyncio.to_thread from the async scorer."""
    if not keys or not cache_enabled():
        return None
    try:
        from ..db import SessionLocal
        from ..models import TriageEnrichmentCache
    except Exception:  # noqa: BLE001
        return None
    db = None
    try:
        db = SessionLocal()
        for key in keys:
            row = db.get(TriageEnrichmentCache, key)
            if row is None:
                continue
            if not _is_fresh(getattr(row, "fetched_at", None)):
                continue
            try:
                raw = RawEvidence.from_dict(json.loads(row.evidence or "{}"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue  # corrupt entry → treat as miss, try next key
            if raw.is_empty():
                continue
            COUNTERS.incr("triage_enrich_cache_hit")
            return raw
        COUNTERS.incr("triage_enrich_cache_miss")
        return None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if db is not None:
            db.close()


def cache_put(keys: list[str], raw: RawEvidence, *, source: str = "unipile") -> None:
    """Write-through: store `raw` under every strong key. No-op when disabled, on
    empty/throttle-stripped evidence, or on any error (fail-soft).

    Upserts via Session.merge (PK = cache_key) so two replicas racing the same
    person can't collide. Opens + closes its own session and commits independently
    of the caller's transaction. Run via asyncio.to_thread."""
    if not keys or not cache_enabled() or raw is None:
        return
    # Quality guard — never freeze a gutted or throttle-stripped pull across
    # events (mirrors the enrichment_raw freeze guard in score.py).
    if raw.is_empty() or getattr(raw.person, "work_unreliable", False):
        return
    try:
        from ..db import SessionLocal
        from ..models import TriageEnrichmentCache
    except Exception:  # noqa: BLE001
        return
    db = None
    try:
        blob = json.dumps(raw.as_dict())
        now = datetime.now(timezone.utc)
        db = SessionLocal()
        for key in keys:
            db.merge(TriageEnrichmentCache(
                cache_key=key, evidence=blob, source=source, fetched_at=now))
        db.commit()
        COUNTERS.incr("triage_enrich_cache_put")
    except Exception:  # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return
    finally:
        if db is not None:
            db.close()
