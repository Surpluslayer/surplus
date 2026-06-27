"""
Tests for Milestone 4 : relationship history feeding the draft/compose prompt.

Covers relationship_context() (compact, outbound-safe, private_note-free) and
its optional wiring into outreach.compose() / _compose_user_message(). Compose
must stay backward-compatible : no history => identical behavior.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.agents.relationship.spine import relationships as rel
from backend.agents import outreach


def _dt(days_ago=0):
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _event(**kw):
    base = dict(id=7, event_name=None, label="Founders Dinner", city="SF",
                kind="in_person", role="eng", seniority="Staff+", co_stage="Seed",
                headcount=20, format="Dinner", goal="Hiring pipeline", budget=0,
                brief="")
    base.update(kw)
    return SimpleNamespace(**base)


def _log(state, ts):
    return SimpleNamespace(state=state, ts=ts, channel="linkedin", body="",
                           provider=None, provider_lead_id=None)


def _prospect(**kw):
    base = dict(
        id=1, name="Maya Rodriguez", role="Staff Infra", company="Lo91r",
        event=_event(), captured_at=_dt(5), source="scan",
        note="talked KV-cache", private_note=None, contact_type=None,
        next_step=None, connection_status="unknown", outreach=[], conversion=None,
        linkedin_url="https://linkedin.com/in/maya", contact_id=None,
        works_on="infra", offers="", headline=None, bio=None, recent_activity=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── relationship_context shape + safety ──────────────────────────────────

def test_context_none_for_fresh_capture():
    # A bare capture with no follow-up signal yields no context block.
    p = _prospect(note=None)
    assert rel.relationship_context(p) is None


def test_context_present_once_there_is_history():
    p = _prospect(captured_at=_dt(10), contact_type="sponsor",
                  next_step="send demo video",
                  outreach=[_log("invite_sent", _dt(2))])
    ctx = rel.relationship_context(p)
    assert ctx is not None
    assert "Contact type: sponsor" in ctx
    assert "send demo video" in ctx
    assert "Last touch: invite sent" in ctx
    assert "Relationship stage: contacted" in ctx


def test_context_excludes_private_note():
    p = _prospect(private_note="budget approver, push hard on price",
                  contact_type="sales")
    ctx = rel.relationship_context(p) or ""
    assert "budget approver" not in ctx
    assert "push hard" not in ctx


def test_context_includes_recent_non_private_summaries():
    p = _prospect(outreach=[_log("message_replied", _dt(1))])
    ctx = rel.relationship_context(p)
    assert "Recent touches:" in ctx


def test_context_is_bounded():
    logs = [_log("invite_sent", _dt(10 - i)) for i in range(8)]
    p = _prospect(outreach=logs)
    ctx = rel.relationship_context(p, max_recent=3)
    # at most 3 recent bullet lines
    assert ctx.count("    · ") <= 3


# ── compose() wiring (backward compatible) ────────────────────────────────

def test_compose_user_message_appends_relationship_block():
    p = _prospect()
    ev = _event()
    msg = outreach._compose_user_message(
        p, ev, host_bio=None, framing="a dinner",
        relationship_ctx="PRIOR RELATIONSHIP (background only):\n- Contact type: sponsor",
    )
    assert "PRIOR RELATIONSHIP" in msg
    assert "Contact type: sponsor" in msg


def test_compose_user_message_unchanged_without_ctx():
    p = _prospect()
    ev = _event()
    msg = outreach._compose_user_message(p, ev, host_bio=None, framing="a dinner")
    assert "PRIOR RELATIONSHIP" not in msg


def test_compose_still_works_with_no_history(monkeypatch):
    # No API key -> template path; relationship_ctx None -> identical output.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = _prospect(note=None)
    ev = _event()
    msg_plain = outreach.compose(p, ev)
    msg_ctx = outreach.compose(p, ev, relationship_ctx=None)
    assert msg_plain.note == msg_ctx.note
    assert msg_plain.message == msg_ctx.message


def test_compose_template_path_ignores_ctx(monkeypatch):
    # Template fallback never reads the prompt, so ctx can't leak into output.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = _prospect(private_note="SECRET MEMO")
    ev = _event()
    msg = outreach.compose(p, ev,
                           relationship_ctx="PRIOR RELATIONSHIP\n- SECRET MEMO")
    assert "SECRET MEMO" not in msg.note
    assert "SECRET MEMO" not in msg.message
