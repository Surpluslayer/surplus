"""Tests for agents/live_enrich.py : grounding outreach in real LinkedIn.

Covers the per-prospect profile enrichment + the host-voice sampling, plus
their idempotency (cached via enriched_at / voice_synced_at) and the
fallback-preserving merge (Exa data is kept when Unipile returns nothing).
"""
from __future__ import annotations
import json
from types import SimpleNamespace

from backend.agents import live_enrich


class _FakeProvider:
    def __init__(self, profile=None, sent=None, dry_run=False):
        self._profile = profile or {}
        self._sent = sent or []
        self.dry_run = dry_run
        self.profile_calls = 0

    def fetch_profile(self, linkedin_url):
        self.profile_calls += 1
        return self._profile

    def fetch_recent_sent_messages(self, limit=20):
        return self._sent[:limit]


def _prospect(**kw):
    base = dict(linkedin_url="https://linkedin.com/in/sam", headline=None,
                bio=None, recent_activity=None, enriched_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── per-prospect enrichment ───────────────────────────────────────────

def test_enrich_prospect_populates_from_live_profile():
    p = _prospect()
    prov = _FakeProvider(profile={
        "headline": "Founding Engineer @ Acme",
        "summary": "Building low-latency LLM serving.",
        "recent_posts": ["Shipped a 3x faster KV-cache today.", "Hiring infra folks."],
    })
    assert live_enrich.enrich_prospect(p, prov) is True
    assert p.headline == "Founding Engineer @ Acme"
    assert p.bio == "Building low-latency LLM serving."
    assert "3x faster KV-cache" in p.recent_activity
    assert "Hiring infra folks" in p.recent_activity
    assert p.enriched_at is not None


def test_enrich_prospect_is_idempotent_and_cached():
    p = _prospect()
    prov = _FakeProvider(profile={"headline": "X"})
    live_enrich.enrich_prospect(p, prov)
    assert prov.profile_calls == 1
    # Second call is a no-op : enriched_at already set, no re-fetch.
    assert live_enrich.enrich_prospect(p, prov) is False
    assert prov.profile_calls == 1


def test_enrich_prospect_keeps_exa_fallback_when_unipile_empty():
    # Discovery (Exa) already set headline/bio; Unipile returns nothing.
    p = _prospect(headline="Exa headline", bio="Exa bio")
    prov = _FakeProvider(profile={})
    live_enrich.enrich_prospect(p, prov)
    assert p.headline == "Exa headline"
    assert p.bio == "Exa bio"
    # Still marked enriched so we don't re-hit Unipile for an empty result.
    assert p.enriched_at is not None


def test_enrich_prospect_survives_provider_error():
    class _Boom:
        def fetch_profile(self, url):
            raise RuntimeError("unipile down")
    p = _prospect(headline="Exa headline")
    # Must not raise; falls back to existing data.
    assert live_enrich.enrich_prospect(p, _Boom()) is True
    assert p.headline == "Exa headline"


# ── host voice sampling ───────────────────────────────────────────────

def test_sync_host_voice_populates_examples():
    u = SimpleNamespace(voice_examples="", voice_synced_at=None)
    prov = _FakeProvider(sent=[
        "Hey Maya, loved your post on inference infra. Building something "
        "similar, worth a quick chat?",
        "ok",  # too short : filtered out
        "Hi Jordan, your work scaling the data platform is exactly the kind "
        "of thing we're gathering folks around. Open to comparing notes?",
    ])
    live_enrich.sync_host_voice(u, prov)
    examples = json.loads(u.voice_examples)
    assert len(examples) == 2  # the 2-char "ok" was dropped
    assert u.voice_synced_at is not None


def test_sync_host_voice_does_not_clobber_curated():
    u = SimpleNamespace(voice_examples=json.dumps(["my curated example"]),
                        voice_synced_at=None)
    prov = _FakeProvider(sent=["some real sent message that is long enough"])
    live_enrich.sync_host_voice(u, prov)
    # Manually-curated examples are preserved; sync just stamps the marker.
    assert json.loads(u.voice_examples) == ["my curated example"]
    assert u.voice_synced_at is not None


def test_sync_host_voice_is_idempotent():
    u = SimpleNamespace(voice_examples="", voice_synced_at="already")
    prov = _FakeProvider(sent=["a long enough real message goes right here"])
    live_enrich.sync_host_voice(u, prov)
    assert u.voice_examples == ""  # untouched : already synced


# ── live-provider gating ──────────────────────────────────────────────

def test_live_provider_none_when_no_account():
    assert live_enrich._live_provider_for_user(
        SimpleNamespace(unipile_account_id=None)) is None
