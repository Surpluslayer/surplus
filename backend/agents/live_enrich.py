"""agents/live_enrich.py : ground outreach in REAL LinkedIn data.

Two pulls, both best-effort and cached so we never re-hit Unipile for the
same target:

  1. Per-prospect : their live LinkedIn profile + recent posts. Makes the
     connection note reference something true and current about THEM instead
     of ICP-derived guesses (seeks/offers). Cached via Prospect.enriched_at.

  2. Per-host : a sample of the host's own recent sent messages, used as
     voice examples so composed outreach sounds like the host. Cached via
     User.voice_synced_at. Never overwrites manually-curated voice_examples.

Both are gated on a LIVE (non-dry-run) provider with an account_id : you
can't read live LinkedIn without a real connected account. In dry-run /
demo, enrichment is skipped and compose falls back to discovery-time (Exa)
data + configured voice examples — so a demo never shows "[dry-run]" text.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Voice-sample relevance (deterministic) ──────────────────────────────
# The host's outbound messages include Surplus's OWN auto-composed sends. If
# those land in the voice pool the composer learns to imitate its own
# templates (a self-reinforcing loop), so we drop anything Surplus has sent on
# the host's behalf. Everything else the host actually typed is kept.
_VOICE_MIN_LEN = 25      # original noise gate : skip one-word replies


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _relevant_voice_samples(messages, sent_bodies) -> list[str]:
    """Filter raw outbound messages down to host-authored ones.

    Drops anything Surplus itself sent (matched against the host's OutreachLog
    bodies) plus trivially-short noise; keeps everything else verbatim.
    """
    sent_norm = {_norm(b) for b in (sent_bodies or set()) if (b or "").strip()}
    seen: set[str] = set()
    out: list[str] = []
    for m in messages or []:
        t = (m or "").strip()
        if not t or len(t) <= _VOICE_MIN_LEN:
            continue
        n = _norm(t)
        if n in sent_norm:      # our own automation output : never voice
            continue
        if n in seen:           # collapse exact repeats
            continue
        seen.add(n)
        out.append(t)
    return out


def enrich_prospect(prospect, provider) -> bool:
    """Populate headline / bio / recent_activity from the prospect's live
    LinkedIn. Idempotent : a no-op once enriched_at is set. Returns True if
    this call performed a fresh enrichment.

    Exa-sourced headline/bio (set at discovery) are kept as the fallback :
    we only overwrite a field when Unipile actually returned a value for it.
    """
    if getattr(prospect, "enriched_at", None) is not None:
        return False
    try:
        prof = provider.fetch_profile(getattr(prospect, "linkedin_url", "") or "")
    except Exception:  # noqa: BLE001 - enrichment must never break outreach
        prof = {}
    if not isinstance(prof, dict):
        prof = {}

    headline = (prof.get("headline") or "").strip()
    if headline:
        prospect.headline = headline[:300]
    summary = (prof.get("summary") or "").strip()
    position = (prof.get("position") or "").strip()
    # Prefer the richer About section; fall back to current position only when
    # we have nothing better than the Exa snippet already on the row.
    if summary:
        prospect.bio = summary
    elif position and not (getattr(prospect, "bio", "") or "").strip():
        prospect.bio = position
    posts = [p for p in (prof.get("recent_posts") or []) if (p or "").strip()]
    if posts:
        prospect.recent_activity = "\n".join(posts)[:2000]

    prospect.enriched_at = _utcnow()
    return True


def sync_host_voice(user, provider, sent_bodies=None) -> None:
    """Auto-populate user.voice_examples from the host's real LinkedIn sent
    messages so composed outreach matches their voice. Idempotent via
    voice_synced_at.

    Never clobbers manually-curated examples : if voice_examples is already
    set, we just stamp voice_synced_at so the auto-sync stays out of the way.

    sent_bodies : the host's own OutreachLog bodies (what Surplus has sent on
    their behalf). Passed by the caller (which holds the DB session) so we can
    drop our own automated sends from the voice pool : otherwise the composer
    learns to imitate its own templates.
    """
    if getattr(user, "voice_synced_at", None) is not None:
        return
    if (getattr(user, "voice_examples", "") or "").strip():
        user.voice_synced_at = _utcnow()
        return
    try:
        # Over-fetch : relevance filtering below will discard most of these,
        # so pull a wide sample to still land ~8 good ones.
        msgs = provider.fetch_recent_sent_messages(limit=40)
    except Exception:  # noqa: BLE001
        msgs = []
    samples = _relevant_voice_samples(msgs, sent_bodies)[:8]
    if samples:
        user.voice_examples = json.dumps(samples)
    user.voice_synced_at = _utcnow()


def _host_sent_bodies(db, user) -> set[str]:
    """Every message body Surplus has sent on this host's behalf (across all
    their events' prospects). Used to drop our own automated sends from the
    voice pool. Best-effort : any failure returns an empty set."""
    if user is None or getattr(user, "id", None) is None:
        return set()
    try:
        from .. import models
        rows = (db.query(models.OutreachLog.body)
                  .join(models.Prospect,
                        models.OutreachLog.prospect_id == models.Prospect.id)
                  .join(models.Event,
                        models.Prospect.event_id == models.Event.id)
                  .filter(models.Event.user_id == user.id)
                  .all())
        return {r[0] for r in rows if r and r[0]}
    except Exception:  # noqa: BLE001
        return set()


def _live_provider_for_user(user):
    """Return a LIVE (non-dry-run) provider for this user, or None when live
    enrichment isn't possible (no connected account / dry-run / misconfig)."""
    if not getattr(user, "unipile_account_id", None):
        return None
    try:
        from ..providers import get_provider_for_user
        provider = get_provider_for_user(user)
    except Exception:  # noqa: BLE001
        return None
    if getattr(provider, "dry_run", True):
        return None
    return provider


async def enrich_then_prefetch(event_id: int, prospect_ids: list[int],
                               user_id: int | None) -> None:
    """Background orchestrator launched after prospecting.

    Opens its own DB session (the request session is gone by the time this
    runs), enriches the host voice + each prospect from live LinkedIn, then
    warms the compose cache so the auto-outreach screen renders relevant,
    on-voice notes immediately.

    Best-effort throughout : any failure falls back to composing on whatever
    data is already on the rows (Exa discovery data + configured voice).
    """
    import asyncio
    from ..db import SessionLocal
    from .. import models
    from .outreach import prefetch_compose_all

    def _enrich_sync() -> tuple[list, object, str]:
        db = SessionLocal()
        try:
            event = db.get(models.Event, event_id)
            if event is None:
                return [], None, ""
            user = db.get(models.User, user_id) if user_id else None
            provider = _live_provider_for_user(user) if user else None
            if provider is not None:
                try:
                    sync_host_voice(user, provider,
                                    sent_bodies=_host_sent_bodies(db, user))
                except Exception:  # noqa: BLE001
                    pass
            prospects = (db.query(models.Prospect)
                           .filter(models.Prospect.id.in_(prospect_ids))
                           .all()) if prospect_ids else []
            if provider is not None:
                for p in prospects:
                    try:
                        enrich_prospect(p, provider)
                    except Exception:  # noqa: BLE001
                        pass
            db.commit()
            voice_raw = (getattr(user, "voice_examples", "") or "") if user else ""
            # Detach fully-loaded rows so the compose pass can read them after
            # we close the session.
            for p in prospects:
                db.refresh(p)
            db.expunge_all()
            return prospects, event, voice_raw
        finally:
            db.close()

    prospects, event, voice_raw = await asyncio.to_thread(_enrich_sync)
    if not prospects or event is None:
        return
    await prefetch_compose_all(prospects, event, voice_examples_raw=voice_raw)
