"""
Tests for agents/relationships.py : the schema-free relationship timeline +
summary built from existing persisted data (Prospect capture metadata,
OutreachLog, Conversion).

Uses lightweight SimpleNamespace stand-ins (the module reads everything via
getattr), matching the repo's pure-function test style. No DB, no network.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.agents.relationship.spine import relationships as rel


def _dt(days_ago=0):
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _event(**kw):
    base = dict(id=7, event_name="Founders Dinner", label=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _log(state, ts, channel="linkedin", body="", provider=None, provider_lead_id=None):
    return SimpleNamespace(state=state, ts=ts, channel=channel, body=body,
                           provider=provider, provider_lead_id=provider_lead_id)


def _conv(state="won", label="Sponsor", detail="$8k booth", value=8000,
          goal="sponsorship", tier="high"):
    return SimpleNamespace(state=state, label=label, detail=detail, value=value,
                           goal=goal, tier=tier)


def _prospect(**kw):
    base = dict(
        id=1, name="Maya Rodriguez", role="Staff Infra", company="Lo91r",
        event=_event(), captured_at=_dt(1), source="scan",
        note=None, private_note=None, contact_type=None, next_step=None,
        connection_status="unknown", outreach=[], conversion=None,
        linkedin_url="https://linkedin.com/in/maya",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _types(timeline):
    return [(it["source_type"], it["interaction_type"]) for it in timeline]


# ── timeline content ──────────────────────────────────────────────────

def test_timeline_includes_capture_event():
    tl = rel.build_timeline(_prospect())
    cap = [it for it in tl if it["source_type"] == "in_person_capture"]
    assert len(cap) == 1
    assert cap[0]["channel"] == "in_person"
    assert cap[0]["metadata"]["event_title"] == "Founders Dinner"


def test_timeline_includes_note():
    tl = rel.build_timeline(_prospect(note="Talked about KV-cache tricks"))
    notes = [it for it in tl if it["interaction_type"] == "note"]
    assert notes and notes[0]["summary"] == "Talked about KV-cache tricks"
    assert notes[0]["metadata"]["private"] is False


def test_timeline_includes_private_note_flagged_private():
    tl = rel.build_timeline(_prospect(private_note="Budget approver, push hard"))
    priv = [it for it in tl if it["interaction_type"] == "private_note"]
    assert priv and priv[0]["metadata"]["private"] is True


def test_timeline_includes_next_step():
    tl = rel.build_timeline(_prospect(next_step="send demo video"))
    ns = [it for it in tl if it["source_type"] == "next_step"]
    assert ns and ns[0]["summary"] == "send demo video"


def test_timeline_includes_outreach_rows():
    p = _prospect(outreach=[
        _log("invite_sent", _dt(3)),
        _log("invite_accepted", _dt(2)),
        _log("message_sent", _dt(1)),
    ])
    tl = rel.build_timeline(p)
    states = [it["interaction_type"] for it in tl if it["source_type"] == "linkedin_outreach"]
    assert states == ["invite_sent", "invite_accepted", "message_sent"]
    assert all(it["direction"] == "outbound"
               for it in tl if it["source_type"] == "linkedin_outreach")


def test_timeline_inbound_reply_marked_inbound():
    p = _prospect(outreach=[_log("message_replied", _dt(1), body="sure, lets chat")])
    tl = rel.build_timeline(p)
    reply = [it for it in tl if it["interaction_type"] == "message_replied"][0]
    assert reply["direction"] == "inbound"


def test_timeline_includes_conversion_row():
    tl = rel.build_timeline(_prospect(conversion=_conv()))
    conv = [it for it in tl if it["source_type"] == "conversion"]
    assert conv and conv[0]["channel"] == "roi"
    assert conv[0]["metadata"]["value"] == 8000


def test_timeline_ordering_is_chronological_and_stable():
    p = _prospect(
        captured_at=_dt(5), note="fun fact", next_step="follow up",
        outreach=[_log("invite_sent", _dt(3)), _log("message_sent", _dt(1))],
        conversion=_conv(),
    )
    tl = rel.build_timeline(p)
    # Capture-time items first (shared ts, tiebroken by source rank), then
    # outreach in time order, conversion (timeless) last.
    assert _types(tl) == [
        ("in_person_capture", "captured"),
        ("manual_note", "note"),
        ("next_step", "next_step"),
        ("linkedin_outreach", "invite_sent"),
        ("linkedin_outreach", "message_sent"),
        ("conversion", "won"),
    ]
    # Deterministic : same input, same order.
    assert _types(rel.build_timeline(p)) == _types(tl)


def test_timeline_empty_prospect_does_not_crash():
    bare = SimpleNamespace(id=2, name="Sam", event=None, captured_at=None,
                           source=None, note=None, private_note=None,
                           next_step=None, contact_type=None,
                           outreach=[], conversion=None)
    assert rel.build_timeline(bare) == []


def test_timeline_naive_datetimes_do_not_break_sorting():
    naive = datetime.now() - timedelta(days=2)  # no tzinfo
    p = _prospect(captured_at=naive, outreach=[_log("invite_sent", naive)])
    tl = rel.build_timeline(p)  # must not raise on mixed naive/aware
    assert all(it["occurred_at"].tzinfo is not None
               for it in tl if it["occurred_at"] is not None)


# ── relationship summary stage logic ────────────────────────────────────

def test_summary_stage_captured_when_only_capture():
    s = rel.relationship_summary(_prospect())
    assert s["relationship_stage"] == "captured"
    assert s["latest_outreach_status"] is None
    assert s["source_event_title"] == "Founders Dinner"


def test_summary_stage_contacted_when_outreach_exists():
    p = _prospect(outreach=[_log("invite_sent", _dt(1))])
    s = rel.relationship_summary(p)
    assert s["relationship_stage"] == "contacted"
    assert s["latest_outreach_status"] == "invite_sent"


def test_summary_stage_replied_on_inbound():
    p = _prospect(outreach=[_log("invite_sent", _dt(2)), _log("message_replied", _dt(1))])
    s = rel.relationship_summary(p)
    assert s["relationship_stage"] == "replied"


def test_summary_stage_converted_wins():
    p = _prospect(outreach=[_log("message_replied", _dt(1))], conversion=_conv("won"))
    s = rel.relationship_summary(p)
    assert s["relationship_stage"] == "converted"
    assert s["conversion_status"] == "won"


def test_summary_stage_stale_when_quiet_too_long():
    p = _prospect(captured_at=_dt(40), outreach=[_log("invite_sent", _dt(40))])
    s = rel.relationship_summary(p)
    assert s["relationship_stage"] == "stale"


def test_summary_stale_does_not_override_converted_or_replied():
    p = _prospect(captured_at=_dt(40), outreach=[_log("message_replied", _dt(40))])
    s = rel.relationship_summary(p)
    assert s["relationship_stage"] == "replied"  # replied beats stale


def test_summary_surfaces_capture_fields():
    p = _prospect(contact_type="sponsor", next_step="send deck",
                  private_note="secret")
    s = rel.relationship_summary(p)
    assert s["contact_type"] == "sponsor"
    assert s["next_step"] == "send deck"
    assert s["has_private_note"] is True


def test_summary_last_touch_tracks_latest_timestamped_item():
    p = _prospect(captured_at=_dt(5), outreach=[_log("invite_sent", _dt(2))])
    s = rel.relationship_summary(p)
    # invite at 2 days ago is more recent than capture at 5 days ago.
    assert s["last_touch_type"] == "invite_sent"


# ── identity ("who they are", from LinkedIn enrichment) ──────────────────

def test_summary_identity_surfaces_enrichment():
    p = _prospect(headline="Founding eng @ Acme · ex-Stripe",
                  works_on="inference infra", bio="Builds fast LLM serving.",
                  recent_activity="Posted about KV-cache eviction.")
    ident = rel.relationship_summary(p)["identity"]
    assert ident["name"] == "Maya Rodriguez"
    assert ident["headline"] == "Founding eng @ Acme · ex-Stripe"
    assert ident["works_on"] == "inference infra"
    assert ident["bio"] == "Builds fast LLM serving."
    assert ident["recent_activity"] == "Posted about KV-cache eviction."


def test_summary_identity_drops_placeholder_defaults():
    # works_on defaults to "general", bio/headline default to None : an
    # un-enriched row must read as empty, not surface the placeholder.
    p = _prospect(works_on="general", headline=None, bio=None, recent_activity=None)
    ident = rel.relationship_summary(p)["identity"]
    assert ident["works_on"] is None
    assert ident["headline"] is None


# ── how_we_met (capture context) ──────────────────────────────────────────

def test_summary_how_we_met_captures_context():
    p = _prospect(event=_event(event_name="Founders Dinner", city="SF"),
                  source="scan", note="talked KV-cache", captured_at=_dt(3))
    hwm = rel.relationship_summary(p)["how_we_met"]
    assert hwm["event_title"] == "Founders Dinner"
    assert hwm["event_city"] == "SF"
    assert hwm["via"] == "scan"
    assert hwm["context"] == "talked KV-cache"
    assert hwm["captured_at"].tzinfo is not None


def test_summary_how_we_met_handles_bare_capture():
    bare = SimpleNamespace(id=9, name="Sam", role=None, company=None, event=None,
                           captured_at=None, source=None, note=None,
                           private_note=None, contact_type=None, next_step=None,
                           connection_status="unknown", outreach=[], conversion=None,
                           linkedin_url=None, headline=None, bio=None,
                           recent_activity=None, works_on=None)
    hwm = rel.relationship_summary(bare)["how_we_met"]
    assert hwm["event_title"] is None
    assert hwm["context"] is None
    assert hwm["captured_at"] is None
