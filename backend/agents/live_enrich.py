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
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def sync_host_voice(user, provider) -> None:
    """Auto-populate user.voice_examples from the host's real LinkedIn sent
    messages so composed outreach matches their voice. Idempotent via
    voice_synced_at.

    Never clobbers manually-curated examples : if voice_examples is already
    set, we just stamp voice_synced_at so the auto-sync stays out of the way.
    """
    if getattr(user, "voice_synced_at", None) is not None:
        return
    if (getattr(user, "voice_examples", "") or "").strip():
        user.voice_synced_at = _utcnow()
        return
    try:
        msgs = provider.fetch_recent_sent_messages(limit=8)
    except Exception:  # noqa: BLE001
        msgs = []
    # Keep substantive messages only : one-word replies ("thanks!", "sounds
    # good") are noise for voice matching. Stamp channel provenance so scoped
    # retrieval (agents/voice.select_voice_records) can tell these LinkedIn-
    # sourced examples apart from any future email/other-channel ones; no
    # message_type since a sent message can be a cold intro OR a follow-up.
    samples = [{"text": m.strip(), "channel": "linkedin"}
               for m in (msgs or []) if len(m.strip()) > 25][:8]
    if samples:
        user.voice_examples = json.dumps(samples)
    user.voice_synced_at = _utcnow()


# Connected Unipile accounts that must NEVER be used to read LinkedIn, no
# matter how the global switch is set. These belong to real people whose
# accounts we will not risk to LinkedIn's anti-automation defenses.
#   UibBNwdySzWz5RBV4rOGkw -> Jia (user 171)
_ALWAYS_BLOCKED_ACCOUNTS = frozenset({"UibBNwdySzWz5RBV4rOGkw"})


def _linkedin_read_disabled_for(account_id: str) -> bool:
    """True when live LinkedIn reads (voice sync + prospect enrichment) must be
    suppressed for this connected account, to avoid tripping LinkedIn's
    anti-scraping defenses on a host's real account.

    Resolution order:
      1. _ALWAYS_BLOCKED_ACCOUNTS : hard-pinned accounts (e.g. Jia) — never read,
         independent of any env switch.
      2. LINKEDIN_READ_DISABLE (default True) : global kill; reads are suppressed
         for everyone unless explicitly set to false.
      3. LINKEDIN_READ_DISABLE_ACCOUNTS : extra comma-separated account ids to
         suppress even if reads are globally re-enabled.

    With reads suppressed, compose falls back to Exa discovery data + the host's
    manually-set voice_examples — i.e. only the input the host provides.
    """
    import os
    from ..providers.unipile import _env_bool
    if account_id in _ALWAYS_BLOCKED_ACCOUNTS:
        return True
    if _env_bool("LINKEDIN_READ_DISABLE", True):
        return True
    blocked = os.environ.get("LINKEDIN_READ_DISABLE_ACCOUNTS", "")
    ids = {a.strip() for a in blocked.split(",") if a.strip()}
    return bool(account_id) and account_id in ids


def _live_provider_for_user(user):
    """Return a LIVE (non-dry-run) provider for this user, or None when live
    enrichment isn't possible (no connected account / dry-run / misconfig) or
    when LinkedIn reads are explicitly disabled for this account."""
    account_id = getattr(user, "unipile_account_id", None)
    if not account_id:
        return None
    if _linkedin_read_disabled_for(account_id):
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
                    sync_host_voice(user, provider)
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
