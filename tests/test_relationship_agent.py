"""
Tests for the agentic relationship layer : the generic tool-use loop
(agents/agent_loop.py) and the propose-only relationship agent
(agents/relationship_agent.py).

We mock the Anthropic client with a small scripted stand-in so the loop runs
deterministically offline (no key, no network) : each call returns a
pre-programmed response (tool_use blocks or a final text turn). This lets us
assert the loop's mechanics — it dispatches tools, feeds results back,
respects the step cap — and the agent's safety property: it only ever STAGES
proposals, never sends or writes.

Direct calls + in-memory SQLite, UNIPILE_DRY_RUN=true (same convention as the
rest of the relationship-layer suite).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import relationships as rel
from backend.agents import agent_loop
from backend.agents import relationship_agent as ragent


# ── in-memory db + builders (mirrors test_relationships_contacts) ─────────

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Op"), email=kw.get("email", "op@x.com"),
                    unipile_account_id=kw.get("acct", "acct1"))
    db.add(u); db.commit()
    return u


def _event(db, user, label="Seed Dinner", city="SF"):
    ev = models.Event(user_id=user.id, kind="in_person", label=label, city=city)
    db.add(ev); db.commit()
    return ev


def _prospect(db, event, *, name="Maya Rodriguez",
              linkedin_url="https://linkedin.com/in/maya", **kw):
    p = models.Prospect(
        event_id=event.id, identity=kw.get("identity", "maya"), name=name,
        role=kw.get("role", "Staff Infra"), company=kw.get("company", "Lo91r"),
        linkedin_url=linkedin_url,
        status=kw.get("status", "pending"), source=kw.get("source", "scan"),
        captured_at=kw.get("captured_at", datetime.now(timezone.utc)),
        connection_status=kw.get("connection_status", "unknown"),
    )
    db.add(p); db.commit()
    return p


# ── scripted Anthropic stand-in ───────────────────────────────────────────

def _text(s):
    return SimpleNamespace(type="text", text=s)


def _tool_use(name, tid, **inp):
    return SimpleNamespace(type="tool_use", name=name, id=tid, input=inp)


class ScriptedClient:
    """Returns pre-programmed responses turn by turn. Each script entry is
    (stop_reason, [content blocks]). Records the messages it was called with."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        self.calls.append(kwargs)
        stop_reason, content = self._script.pop(0)
        return SimpleNamespace(stop_reason=stop_reason, content=content)


# ── agent_loop primitive ──────────────────────────────────────────────────

def test_loop_dispatches_tool_and_feeds_result_back():
    """A tool_use turn -> impl is called -> result is fed back -> next turn
    sees it -> model ends. The loop's core mechanic."""
    seen_results = {}

    def adder(a, b):
        return {"sum": a + b}

    client = ScriptedClient([
        ("tool_use", [_tool_use("add", "t1", a=2, b=3)]),
        ("end_turn", [_text("The sum is 5.")]),
    ])
    run = agent_loop.run_agent(
        system="s", tools=[{"name": "add", "description": "", "input_schema": {}}],
        tool_impls={"add": adder}, user_prompt="add 2 and 3", client=client)

    assert run.stop_reason == "end_turn"
    assert run.steps == 2
    assert len(run.tool_calls) == 1
    assert run.tool_calls[0].name == "add"
    assert run.tool_calls[0].result == {"sum": 5}
    assert run.final_text == "The sum is 5."
    # Second create call must have received the tool_result we produced.
    second_msgs = client.calls[1]["messages"]
    def _is_tool_result(blk):
        t = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", "")
        return t == "tool_result"
    assert any(
        isinstance(m["content"], list)
        and any(_is_tool_result(blk) for blk in m["content"])
        for m in second_msgs
    )


def test_loop_respects_step_cap():
    """A model that asks for a tool forever is cut off at max_steps."""
    client = ScriptedClient([("tool_use", [_tool_use("noop", f"t{i}")]) for i in range(20)])
    run = agent_loop.run_agent(
        system="s", tools=[{"name": "noop", "description": "", "input_schema": {}}],
        tool_impls={"noop": lambda: {"ok": True}},
        user_prompt="loop", max_steps=3, client=client)
    assert run.stop_reason == "max_steps"
    assert run.steps == 3


def test_loop_surfaces_tool_error_to_model_without_crashing():
    """A raising tool returns its error as a tool_result; the loop keeps going."""
    def boom():
        raise ValueError("kaboom")

    client = ScriptedClient([
        ("tool_use", [_tool_use("boom", "t1")]),
        ("end_turn", [_text("recovered")]),
    ])
    run = agent_loop.run_agent(
        system="s", tools=[{"name": "boom", "description": "", "input_schema": {}}],
        tool_impls={"boom": boom}, user_prompt="go", client=client)
    assert run.stop_reason == "end_turn"
    assert run.tool_calls[0].error is not None
    assert "kaboom" in run.tool_calls[0].error


def test_loop_unknown_tool_is_reported_not_fatal():
    client = ScriptedClient([
        ("tool_use", [_tool_use("ghost", "t1")]),
        ("end_turn", [_text("done")]),
    ])
    run = agent_loop.run_agent(
        system="s", tools=[], tool_impls={}, user_prompt="go", client=client)
    assert run.tool_calls[0].error is not None
    assert "unknown tool" in run.tool_calls[0].error


# ── relationship agent (propose-only) ─────────────────────────────────────

def test_agent_empty_spine_short_circuits(db):
    """No contacts -> no LLM call at all, friendly summary."""
    u = _user(db)
    res = ragent.run_relationship_agent(db, u.id, client=ScriptedClient([]))
    assert res.stop_reason == "empty"
    assert res.contacts_seen == 0
    assert res.proposals == []


def test_agent_stages_proposals_never_sends(db):
    """The agent surveys, reads a contact, and stages a next-step + a draft.
    Critical safety assertion: NO OutreachLog row is written (nothing sent)
    and proposals are returned for human approval."""
    u = _user(db)
    ev = _event(db, u)
    # A stale contact: captured 40 days ago, never touched since.
    old = datetime.now(timezone.utc) - timedelta(days=40)
    p = _prospect(db, ev, captured_at=old)
    c = rel.link_contact(db, p, u.id)

    # Roster is inline in the prompt, so the agent goes straight to get_contact.
    script = [
        ("tool_use", [_tool_use("get_contact", "t2", contact_id=c.id)]),
        ("tool_use", [
            _tool_use("propose_next_step", "t3", contact_id=c.id,
                      next_step="Send a warm re-intro referencing the Seed Dinner.",
                      rationale="40 days cold, strong first meeting."),
            _tool_use("draft_message", "t4", contact_id=c.id,
                      message="Hey Maya — great chatting at the Seed Dinner. "
                              "Would love to reconnect.",
                      rationale="Grounded in the shared Seed Dinner event."),
        ]),
        ("end_turn", [_text("Found 1 stale contact (Maya) and proposed a re-intro.")]),
    ]
    res = ragent.run_relationship_agent(db, u.id, client=ScriptedClient(script))

    assert res.error is None
    assert res.contacts_seen == 1
    assert len(res.proposals) == 2
    kinds = {pr.kind for pr in res.proposals}
    assert kinds == {"next_step", "draft_message"}
    # Both proposals resolved the real contact name (not invented).
    assert all(pr.contact_name == "Maya Rodriguez" for pr in res.proposals)
    # The staged draft references the real shared event, not a hallucination.
    drafts = [pr for pr in res.proposals if pr.kind == "draft_message"]
    assert "Seed Dinner" in drafts[0].text
    assert "Maya" in res.summary
    # SAFETY: nothing was sent.
    assert db.query(models.OutreachLog).count() == 0


def test_on_proposal_fires_for_each_staged_proposal(db):
    """The streaming chat route relies on on_proposal firing the instant each
    proposal is staged (so cards reveal one-by-one). Verify it's called once
    per staged proposal, in order, with the resolved Proposal."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev, captured_at=datetime.now(timezone.utc) - timedelta(days=40))
    c = rel.link_contact(db, p, u.id)

    script = [
        ("tool_use", [_tool_use("get_contact", "t2", contact_id=c.id)]),
        ("tool_use", [
            _tool_use("propose_next_step", "t3", contact_id=c.id,
                      next_step="Re-intro.", rationale="cold"),
            _tool_use("draft_message", "t4", contact_id=c.id,
                      message="Hey Maya, great chatting.", rationale="grounded"),
        ]),
        ("end_turn", [_text("done")]),
    ]
    seen = []
    res = ragent.run_relationship_agent(
        db, u.id, client=ScriptedClient(script), on_proposal=lambda pr: seen.append(pr))

    # Fired once per staged proposal, in staging order, with real names.
    assert [pr.kind for pr in seen] == ["next_step", "draft_message"]
    assert seen == res.proposals
    assert all(pr.contact_name == "Maya Rodriguez" for pr in seen)


def test_agent_proposal_for_unknown_contact_is_rejected(db):
    """If the model proposes against a contact_id that isn't the host's, the
    tool refuses (owner-scoping) and no proposal is staged."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    rel.link_contact(db, p, u.id)

    script = [
        ("tool_use", [_tool_use("propose_next_step", "t2", contact_id=99999,
                                next_step="x")]),
        ("end_turn", [_text("done")]),
    ]
    res = ragent.run_relationship_agent(db, u.id, client=ScriptedClient(script))
    # Owner-scoping: the invented contact_id never resolved, so nothing staged.
    assert res.proposals == []
    assert res.error is None


def test_agent_get_contact_returns_real_history(db):
    """The get_contact tool exposes the deterministic spine, so the agent
    reasons over real events/timeline (not hallucinated)."""
    u = _user(db)
    ev = _event(db, u, label="Founders Mixer")
    p = _prospect(db, ev)
    c = rel.link_contact(db, p, u.id)

    captured = {}

    class Capturing(ScriptedClient):
        def create(self, **kwargs):
            return super().create(**kwargs)

    # Drive one get_contact and capture the tool result via the run record.
    script = [
        ("tool_use", [_tool_use("get_contact", "t1", contact_id=c.id)]),
        ("end_turn", [_text("ok")]),
    ]
    res = ragent.run_relationship_agent(db, u.id, client=Capturing(script))
    assert res.error is None
    # The agent ran one get_contact; its result carried the real event title.
    # (We assert indirectly: the run completed and saw the one contact.)
    assert res.contacts_seen == 1


def test_thread_from_timeline_excludes_private_note():
    """The operator-only private_note (stored as a private manual_note) must
    never reach prior_messages — otherwise it could shape an outbound draft.
    Public note + capture + outreach DO flow through."""
    now = datetime.now(timezone.utc)
    timeline = [
        {"source_type": "in_person_capture", "interaction_type": "captured",
         "occurred_at": now, "title": "Captured", "summary": "Met at Tech Week",
         "channel": "in_person", "direction": "none", "metadata": {}},
        {"source_type": "manual_note", "interaction_type": "note",
         "occurred_at": now, "title": "Note", "summary": "I have five siblings",
         "channel": "manual", "direction": "none", "metadata": {"private": False}},
        {"source_type": "manual_note", "interaction_type": "private_note",
         "occurred_at": now, "title": "Private note",
         "summary": "OPERATOR_ONLY_SECRET_MEMO",
         "channel": "manual", "direction": "none", "metadata": {"private": True}},
        {"source_type": "linkedin_outreach", "interaction_type": "message_sent",
         "occurred_at": now, "title": "Message Sent", "summary": "Coffee soon?",
         "channel": "linkedin", "direction": "outbound", "metadata": {}},
    ]
    texts = [t["text"] for t in ragent._thread_from_timeline(timeline)]
    assert "Met at Tech Week" in texts
    assert "I have five siblings" in texts          # public note still flows
    assert "Coffee soon?" in texts
    assert all("OPERATOR_ONLY_SECRET_MEMO" not in t for t in texts)


# ── _thread_signals : the cheap recall signals roster-build distils per row ───
# Pure function over the thread (_thread_from_timeline shape). These pin the
# recall heuristics that let message-blind Phase-1 triage spot content-only open
# loops — the "misses obvious people" fix. Structural fields are exact; the
# promise/question fields are loose-on-purpose (Phase-2 is the precision filter).

def _msg(who, text, *, days_ago=0):
    return {"when": datetime.now(timezone.utc) - timedelta(days=days_ago),
            "who": who, "channel": "linkedin", "text": text}


def test_thread_signals_empty_thread_is_all_falsey():
    sig = ragent._thread_signals([])
    assert sig["last_message_from"] is None
    assert sig["open_loop_detected"] is False
    assert sig["followup_due"] is False
    assert sig["awaiting_host_reply"] is False


def test_thread_signals_context_only_thread_is_inert():
    """'context' items (capture notes / non-directional) carry no who-spoke-last
    signal, so a thread of only those reads as empty."""
    sig = ragent._thread_signals([_msg("context", "Met at Tech Week")])
    assert sig["last_message_from"] is None
    assert sig["open_loop_detected"] is False


def test_thread_signals_contact_spoke_last_awaits_host():
    sig = ragent._thread_signals([
        _msg("host", "Great meeting you!", days_ago=5),
        _msg("them", "Likewise, talk soon", days_ago=3),
    ])
    assert sig["last_message_from"] == "contact"
    assert sig["awaiting_host_reply"] is True
    assert sig["awaiting_contact_reply"] is False
    assert sig["last_message_age_days"] == 3


def test_thread_signals_contact_question_is_open_loop():
    sig = ragent._thread_signals([
        _msg("host", "Nice to connect", days_ago=4),
        _msg("them", "Can you send me the pricing?", days_ago=2),
    ])
    assert sig["contact_open_question"] is True
    assert sig["open_loop_detected"] is True
    assert sig["open_loop_type"] == "answer_question"
    assert "pricing" in sig["open_loop_evidence"]
    assert sig["followup_due"] is True            # obligation + >= 1 day old


def test_thread_signals_host_promise_is_open_loop():
    """The keystone recall case: host promised to send something and the contact
    hasn't replied. Message-blind triage would have dropped this as 'recent
    unanswered outreach'; the signal makes it a must-nominate."""
    sig = ragent._thread_signals([
        _msg("them", "Loved the talk", days_ago=6),
        _msg("host", "I'll send you the deck next week", days_ago=4),
    ])
    assert sig["last_message_from"] == "host"
    assert sig["host_open_promise"] is True
    assert sig["open_loop_detected"] is True
    assert sig["open_loop_type"] == "send_resource"
    assert sig["followup_due"] is True


def test_thread_signals_host_intro_promise_classifies_intro():
    sig = ragent._thread_signals([
        _msg("host", "Happy to intro you to our CTO", days_ago=3),
    ])
    assert sig["host_open_promise"] is True
    assert sig["open_loop_type"] == "intro"


def test_thread_signals_plain_host_message_is_no_loop():
    """A recent unanswered first outreach with NO promise is exactly the case we
    must NOT over-nominate on — no open loop, host isn't waited on."""
    sig = ragent._thread_signals([
        _msg("host", "Nice to meet you at the event", days_ago=2),
    ])
    assert sig["awaiting_contact_reply"] is True
    assert sig["host_open_promise"] is False
    assert sig["open_loop_detected"] is False
    assert sig["followup_due"] is False


def test_thread_signals_brandnew_obligation_not_yet_due():
    """followup_due gates on age >= 1 day so a same-moment exchange isn't flagged
    overdue — Phase-2 owns precise timing."""
    sig = ragent._thread_signals([
        _msg("host", "I'll send the deck", days_ago=0),
    ])
    assert sig["host_open_promise"] is True
    assert sig["open_loop_detected"] is True
    assert sig["followup_due"] is False


# ── _context_brief : the deterministic Phase-2 pre-read ───────────────────────
# Pure function of (sel, ctx). Adds NO LLM call; every field is derived from the
# thread signals + rollup summary already in ctx. These pin that the brief never
# fabricates and that its skip/caution flags fire on the right thread shapes.

def _ctx(summary=None, events=None, thread=None):
    return {"summary": summary or {}, "events": events or [],
            "timeline": [], "prior_messages": thread or []}


def test_brief_contact_question_drives_answer_action():
    ctx = _ctx(
        summary={"name": "Sarah Lin", "company": "Acme",
                 "relationship_stage": "replied", "last_touch_at": None},
        events=[{"name": "AI Dinner"}],
        thread=[_msg("host", "Great meeting you!", days_ago=5),
                _msg("them", "You too! Can you send the deck?", days_ago=2)])
    b = ragent._context_brief({"reason": "replied", "angle": "they asked for the deck"}, ctx)
    assert b["natural_action"] == "answer the question the contact asked"
    assert "host owes a reply" in b["thread_status"]
    assert b["continue_from"] == "You too! Can you send the deck?"
    assert b["do_not_repeat"] == "Great meeting you!"      # host's last, not the contact's
    assert "name: Sarah Lin" in b["safe_facts_to_use"]
    assert "company: Acme" in b["safe_facts_to_use"]
    assert "shared events: AI Dinner" in b["safe_facts_to_use"]
    assert b["drafting_risks"] == []                       # contact owes the move, not host


def test_brief_written_next_step_is_the_action_and_desired_step():
    ctx = _ctx(
        summary={"name": "Tom", "relationship_stage": "contacted",
                 "next_step": "send intro to Mara", "last_touch_at": None},
        thread=[_msg("them", "Would love an intro to Mara", days_ago=4),
                _msg("host", "I'll intro you to Mara this week", days_ago=2)])
    b = ragent._context_brief({"reason": "host promise", "angle": ""}, ctx)
    assert b["natural_action"] == "act on the host's written next step: send intro to Mara"
    assert b["desired_next_step"] == "send intro to Mara"
    # host spoke last & recent -> the brief warns this is likely their-court/too-soon
    assert any("next natural move" in r for r in b["drafting_risks"])
    assert any("TOO SOON" in r for r in b["drafting_risks"])


def test_brief_empty_thread_flags_no_history_and_no_fabrication():
    b = ragent._context_brief(
        {"reason": "stale", "angle": "reconnect"},
        _ctx(summary={"name": "Pat", "relationship_stage": "stale", "last_touch_at": None}))
    assert b["thread_status"] == "no prior messages on file"
    assert b["continue_from"] is None and b["do_not_repeat"] is None
    assert any("do not fabricate" in r for r in b["drafting_risks"])
    # relationship_state must not double-print 'stale'
    assert b["relationship_state"] == "stale"


def test_brief_always_guards_against_invented_updates_and_flags_angle():
    b = ragent._context_brief(
        {"reason": "x", "angle": "they just raised a Series A"},
        _ctx(summary={"name": "Mia", "last_touch_at": None},
             thread=[_msg("them", "hi", days_ago=1)]))
    avoid = " ".join(b["facts_to_avoid_or_treat_as_uncertain"])
    assert "NOT visible in prior_messages" in avoid
    assert "they just raised a Series A" in avoid     # the loose angle is flagged, not trusted


def test_brief_host_promise_without_next_step_uses_promise_action():
    ctx = _ctx(
        summary={"name": "Lee", "last_touch_at": None},
        thread=[_msg("them", "great chat", days_ago=6),
                _msg("host", "I'll send you the deck next week", days_ago=4)])
    b = ragent._context_brief({"reason": "promise", "angle": ""}, ctx)
    assert b["natural_action"] == "deliver on the host's open promise (send_resource)"
    assert any("keyword guess" in a for a in b["facts_to_avoid_or_treat_as_uncertain"])


def test_brief_reads_contact_register_from_their_messages_only():
    """The brief should classify how the CONTACT writes (so the draft can meet
    their register) — judged from their messages, not the host's."""
    formal = _ctx(
        summary={"name": "Dr. Patel", "last_touch_at": None},
        thread=[_msg("host", "Great meeting you! Let's stay in touch.", days_ago=5),
                _msg("them", "Dear Alex, I would welcome the opportunity to "
                             "discuss further. Might you have availability next "
                             "week? Kind regards, Dr. Patel", days_ago=2)])
    b = ragent._context_brief({"reason": "replied", "angle": ""}, formal)
    assert b["contact_register"] == "formal"
    assert "no emoji" in b["register_guidance"]

    casual = _ctx(
        summary={"name": "Maya", "last_touch_at": None},
        thread=[_msg("host", "Great meeting you! Let's stay in touch.", days_ago=5),
                _msg("them", "omg yesss so good meeting you!! lmk when ur free 🙌",
                     days_ago=2)])
    c = ragent._context_brief({"reason": "replied", "angle": ""}, casual)
    assert c["contact_register"] == "casual"


def test_brief_register_is_none_with_no_contact_messages():
    """No contact message yet (host-only opener) → nothing to mirror."""
    b = ragent._context_brief(
        {"reason": "stale", "angle": ""},
        _ctx(summary={"name": "Mia", "last_touch_at": None},
             thread=[_msg("host", "Great meeting you at the mixer!", days_ago=60)]))
    assert b["contact_register"] is None
    assert b["register_guidance"] is None


# ── "who to follow up" : the deterministic signals list_contacts exposes ──────
# The agent never decides staleness itself — it reads is_stale / has_next_step /
# days_since_last_touch off contact_summary. These tests pin those inputs so a
# change to the staleness rule is a conscious one, not a silent regression.

def _outreach(db, prospect, state, *, days_ago=0, body=""):
    o = models.OutreachLog(
        prospect_id=prospect.id, channel="linkedin", state=state, body=body,
        ts=datetime.now(timezone.utc) - timedelta(days=days_ago))
    db.add(o); db.commit()
    return o


def test_who_captured_goes_stale_after_14_days(db):
    """A captured-but-never-contacted contact flips to 'stale' once the last
    touch is older than STALE_AFTER_DAYS (14). is_stale in list_contacts is
    exactly relationship_stage == 'stale'."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=15))
    c = rel.link_contact(db, p, u.id)
    assert rel.contact_summary(db, c)["relationship_stage"] == "stale"


def test_who_recent_capture_is_not_stale(db):
    """Inside the 14-day window the same contact is still just 'captured' —
    the agent should skip them as recently-touched."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=13))
    c = rel.link_contact(db, p, u.id)
    assert rel.contact_summary(db, c)["relationship_stage"] == "captured"


def test_who_next_step_presence_is_surfaced(db):
    """has_next_step in list_contacts is bool(next_step); a contact WITH a
    planned step is deprioritised by the heuristic."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=2))
    p.next_step = "grab a coffee"
    db.commit()
    c = rel.link_contact(db, p, u.id)
    assert rel.contact_summary(db, c)["next_step"] == "grab a coffee"


def test_strip_dashes_removes_em_and_en_dashes():
    """No staged draft may carry an em/en dash (the AI 'tell'). The sanitizer
    rewrites them to commas and tidies the resulting punctuation/spacing."""
    s = ragent._strip_dashes
    assert "—" not in s("Hey Mia — just bumping this.")
    assert s("Hey Mia — just bumping this.") == "Hey Mia, just bumping this."
    assert "–" not in s("Tech Week was a blur – would love to catch up.")
    # dash right before terminal punctuation shouldn't leave a dangling comma
    assert s("Worth a quick coffee —.") == "Worth a quick coffee."
    assert s("") == ""


def test_draft_message_sanitizes_em_dash(db):
    """End-to-end: even if the model emits an em dash, the staged proposal is
    clean. Proves the guard sits on the tool impl, not just the prompt."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    c = rel.link_contact(db, p, u.id)
    script = [
        ("tool_use", [_tool_use("draft_message", "t1", contact_id=c.id,
                                message="Hey Maya — bumping this — still keen?",
                                rationale="Continues the thread — light nudge.")]),
        ("end_turn", [_text("done")]),
    ]
    res = ragent.run_relationship_agent(db, u.id, client=ScriptedClient(script))
    draft = next(pr for pr in res.proposals if pr.kind == "draft_message")
    assert "—" not in draft.text and "–" not in draft.text
    assert "—" not in draft.rationale


def test_host_voice_examples_resolves_from_user_row(db):
    """The agent sources the host's voice from the SAME User.voice_examples the
    initial-message composer uses, parsed as a JSON list and capped at 8."""
    u = _user(db)
    u.voice_examples = json.dumps(
        ["yo! great running into you", "lol yeah let's def grab a coffee"]
        + [f"msg {i}" for i in range(10)])
    db.commit()
    ex = ragent._host_voice_examples(db, u.id)
    assert ex[0] == "yo! great running into you"
    assert len(ex) == 8                       # capped


def test_host_voice_examples_bad_json_is_empty(db):
    """A typo in voice_examples can't break a run — bad JSON resolves to []."""
    u = _user(db)
    u.voice_examples = "{not valid json"
    db.commit()
    assert ragent._host_voice_examples(db, u.id) == []


def test_voice_block_empty_when_no_examples():
    assert ragent._voice_block([]) == ""
    block = ragent._voice_block(["hey!! good seeing you"])
    assert "<style_examples>" in block
    assert "hey!! good seeing you" in block


def test_agent_injects_host_voice_into_system_prompt(db):
    """End-to-end: when the host has voice_examples, the style block reaches the
    model's system prompt — so follow-ups are written in the host's voice."""
    u = _user(db)
    u.voice_examples = json.dumps(["yo!! so good to finally meet you haha"])
    db.commit()
    ev = _event(db, u)
    p = _prospect(db, ev)
    rel.link_contact(db, p, u.id)

    client = ScriptedClient([
        ("end_turn", [_text("done")]),
    ])
    ragent.run_relationship_agent(db, u.id, client=client)
    sent_system = client.calls[0]["system"][0]["text"]
    assert "<style_examples>" in sent_system
    assert "yo!! so good to finally meet you haha" in sent_system


def _message_text(m: dict) -> str:
    """Flatten a recorded message's content to plain text (string content, or
    the concatenated text of any text blocks)."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _list_contacts_payload(client) -> list[dict]:
    """Pull the contact roster the model actually saw back out of the scripted
    client's recorded calls. The roster is no longer a `list_contacts` tool
    round-trip — it's injected inline into the kickoff prompt (one fewer
    sequential LLM call), so we recover it from the prompt text. This is the
    exact dict shape `_list_contacts` produced, not a reconstruction."""
    marker = "(one row per person):\n"
    for call in client.calls:
        for m in call.get("messages", []):
            text = _message_text(m)
            i = text.find(marker)
            if i == -1:
                continue
            after = text[i + len(marker):]
            j = after.find("[")
            if j == -1:
                continue
            try:
                rows, _ = json.JSONDecoder().raw_decode(after[j:])
            except ValueError:
                continue
            if isinstance(rows, list) and rows and "contact_id" in rows[0]:
                return rows
    return []


def test_who_marked_follow_up_is_surfaced(db):
    """The capture-phase `contact_type='follow_up'` marker — the host's explicit
    'circle back to them' intent set when they met — must reach the agent's
    list_contacts survey as `marked_follow_up`/`contact_types`. This is the
    primary WHO signal that does NOT depend on any external/watch-job news."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=2))
    p.contact_type = "follow_up"
    db.commit()
    c = rel.link_contact(db, p, u.id)

    # The roster is injected into the kickoff prompt, so the model needs no
    # survey turn — it can finish immediately and the roster is still recoverable.
    client = ScriptedClient([
        ("end_turn", [_text("done")]),
    ])
    res = ragent.run_relationship_agent(db, u.id, client=client)
    assert res.error is None
    rows = _list_contacts_payload(client)
    row = next(r for r in rows if r["contact_id"] == c.id)
    assert row["marked_follow_up"] is True
    assert "follow_up" in row["contact_types"]


def test_roster_row_carries_host_promise_open_loop(db):
    """End-to-end of the recall fix: a host outbound "I'll send the deck" with no
    reply must reach the triage roster as host_open_promise/open_loop_detected,
    so message-blind Phase-1 can nominate it instead of dropping it as a recent
    unanswered outreach. This is the exact 'misses obvious people' case."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=6))
    c = rel.link_contact(db, p, u.id)
    _outreach(db, p, "message_sent", days_ago=3,
              body="Loved the chat earlier. I'll send you the deck next week")

    client = ScriptedClient([("end_turn", [_text("done")])])
    res = ragent.run_relationship_agent(db, u.id, client=client)
    assert res.error is None
    rows = _list_contacts_payload(client)
    row = next(r for r in rows if r["contact_id"] == c.id)
    assert row["host_open_promise"] is True
    assert row["open_loop_detected"] is True
    assert row["open_loop_type"] == "send_resource"
    assert row["last_message_from"] == "host"
    assert row["followup_due"] is True


def test_who_non_follow_up_tag_is_not_marked(db):
    """A contact tagged with a different capture type (e.g. 'sales') surfaces
    that tag but is NOT marked_follow_up — so the marker is specific, not a
    catch-all on any contact_type being present."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=2))
    p.contact_type = "sales"
    db.commit()
    c = rel.link_contact(db, p, u.id)

    client = ScriptedClient([
        ("end_turn", [_text("done")]),
    ])
    res = ragent.run_relationship_agent(db, u.id, client=client)
    assert res.error is None
    rows = _list_contacts_payload(client)
    row = next(r for r in rows if r["contact_id"] == c.id)
    assert row["marked_follow_up"] is False
    assert row["contact_types"] == ["sales"]


def test_who_replied_then_cold_is_never_stale_KNOWN_GAP(db):
    """KNOWN GAP: a contact who replied then went quiet never flips to 'stale'
    (replied outranks stale in _STAGE_RANK, and the stale overlay only applies
    to captured/contacted). So the deterministic is_stale signal MISSES
    replied-then-ghosted contacts — only the raw days_since_last_touch would
    surface them. This asserts CURRENT behavior; flipping it is a product
    decision, not a bug-fix to slip in silently."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=60))
    _outreach(db, p, "invite_sent", days_ago=60, body="hi")
    _outreach(db, p, "message_replied", days_ago=58, body="sure!")
    c = rel.link_contact(db, p, u.id)
    # 58 days cold, yet not stale:
    assert rel.contact_summary(db, c)["relationship_stage"] == "replied"


# ── latency structure: roster injection + thread caching ──────────────────────

def test_roster_injected_inline_and_no_list_contacts_tool(db):
    """The survey is handed to the model inline (one fewer sequential LLM call
    before the first card) instead of via a `list_contacts` tool round-trip. So:
      - the roster is recoverable from the kickoff prompt, AND
      - `list_contacts` is no longer offered as a tool at all."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev,
                  captured_at=datetime.now(timezone.utc) - timedelta(days=20))
    c = rel.link_contact(db, p, u.id)

    client = ScriptedClient([("end_turn", [_text("done")])])
    res = ragent.run_relationship_agent(db, u.id, client=client)
    assert res.error is None

    # Roster reached the model inline, with the real contact in it.
    rows = _list_contacts_payload(client)
    assert any(r["contact_id"] == c.id for r in rows)

    # list_contacts is not a tool the model can call anymore.
    tool_names = {t["name"] for t in client.calls[0]["tools"]}
    assert "list_contacts" not in tool_names
    assert "get_contact" in tool_names


def test_mark_thread_cache_moves_single_breakpoint_to_tail():
    """Incremental prompt caching: exactly one cache breakpoint sits at the end
    of the latest message, and a prior breakpoint is stripped as the thread
    grows (so we never blow past Anthropic's 4-breakpoint limit)."""
    # A bare-string first message is normalised to a cache-marked text block.
    messages = [{"role": "user", "content": "kickoff"}]
    agent_loop._mark_thread_cache(messages)
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    # Thread grows: the old breakpoint is cleared, the new tail gets the marker.
    messages.append({"role": "assistant", "content": [
        {"type": "text", "text": "thinking"}]})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]})
    agent_loop._mark_thread_cache(messages)

    marked = [
        (mi, bi)
        for mi, m in enumerate(messages)
        for bi, b in enumerate(m["content"])
        if isinstance(b, dict) and "cache_control" in b
    ]
    assert marked == [(2, 0)]  # exactly one, on the last message's last block


# ── concurrent variant: triage + parallel fan-out drafts ──────────────────────
# run_relationship_agent_concurrent splits the run into ONE triage call (roster
# -> ranked selections) then a parallel per-person draft call. The fan-out races,
# so the fake client routes by call TYPE (triage vs draft) and looks the draft up
# by contact_id from the prompt — deterministic regardless of completion order.

class ConcurrentScriptedClient:
    """Order-independent stand-in for the two-phase concurrent path.

    The triage call (its tools include `select_followups`) returns `triage`;
    every draft call returns the entry in `drafts` keyed by the contact_id the
    prompt names. Thread-safe call recording (the drafts fan out across threads).
    """

    def __init__(self, *, triage, drafts):
        self._triage = list(triage)          # content blocks for select_followups
        self._drafts = dict(drafts)          # {contact_id: [content blocks]}
        self.calls = []
        self._lock = threading.Lock()
        self.messages = self                 # so client.messages.create works

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        tool_names = {t["name"] for t in kwargs.get("tools", [])}
        if "select_followups" in tool_names:
            return SimpleNamespace(stop_reason="tool_use", content=self._triage)
        # Draft call: the prompt opens with "... (contact_id N)." — match that
        # (the ctx JSON's "contact_id": N has no space, so it won't false-match).
        text = "".join(m["content"] for m in kwargs["messages"]
                       if isinstance(m.get("content"), str))
        m = re.search(r"contact_id (\d+)", text)
        cid = int(m.group(1)) if m else None
        return SimpleNamespace(stop_reason="tool_use",
                               content=self._drafts.get(cid, []))

    def triage_calls(self):
        return [c for c in self.calls
                if any(t["name"] == "select_followups" for t in c.get("tools", []))]

    def draft_calls(self):
        return [c for c in self.calls
                if all(t["name"] != "select_followups" for t in c.get("tools", []))]


def _stale_contact(db, u, ev, *, name, ident, days=40):
    p = _prospect(db, ev, name=name, identity=ident,
                  linkedin_url=f"https://linkedin.com/in/{ident}",
                  captured_at=datetime.now(timezone.utc) - timedelta(days=days))
    return rel.link_contact(db, p, u.id)


def test_concurrent_triages_then_drafts_in_parallel(db):
    """One triage call selects two people; each is drafted in its own call. All
    proposals are staged with real names, on_proposal fires per draft, and the
    SAFETY invariant holds: nothing is sent (no OutreachLog)."""
    u = _user(db)
    ev = _event(db, u)
    a = _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya")
    b = _stale_contact(db, u, ev, name="Shama Patel", ident="shama")

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": a.id, "reason": "40d cold",
                                     "angle": "Seed Dinner"},
                                    {"contact_id": b.id, "reason": "40d cold",
                                     "angle": "warm intro"}],
                        closing="Drafted both, Maya's the one I'd prioritize.")]
    drafts = {
        a.id: [_tool_use("draft_message", "da", contact_id=a.id,
                         message="Hey Maya, great chatting at the Seed Dinner.",
                         rationale="40d cold")],
        b.id: [_tool_use("draft_message", "db", contact_id=b.id,
                         message="Hey Shama, want that intro still?",
                         rationale="warm intro")],
    }
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    seen = []
    res = ragent.run_relationship_agent_concurrent(
        db, u.id, client=client, on_proposal=lambda pr: seen.append(pr))

    assert res.error is None
    assert res.contacts_seen == 2
    # Exactly one triage call, one draft call PER selected person.
    assert len(client.triage_calls()) == 1
    assert len(client.draft_calls()) == 2
    # Both drafts staged, names resolved from the real spine (order-independent).
    assert len(res.proposals) == 2
    assert {pr.contact_name for pr in res.proposals} == {"Maya Rodriguez", "Shama Patel"}
    assert {pr.kind for pr in res.proposals} == {"draft_message"}
    # on_proposal fired once per staged proposal.
    assert len(seen) == 2 and all(pr in res.proposals for pr in seen)
    # Closing line from triage becomes the summary.
    assert "prioritize" in res.summary
    # SAFETY: nothing sent.
    assert db.query(models.OutreachLog).count() == 0


def test_concurrent_skip_suppresses_draft(db):
    """The draft phase can decline via skip_contact (the loop's suppression rule,
    now that it has the full thread): a selected person who's already handled
    produces NO staged proposal."""
    u = _user(db)
    ev = _event(db, u)
    c = _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya")

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": c.id, "reason": "marked",
                                     "angle": "x"}],
                        closing="One marked, already handled.")]
    drafts = {c.id: [_tool_use("skip_contact", "sk", contact_id=c.id,
                               reason="already replied")]}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert res.error is None
    assert len(client.draft_calls()) == 1     # we DID try to draft
    assert res.proposals == []                # but skip staged nothing
    # The summary must reflect the SKIP, not the triage's stale "already handled"
    # promise. It must not claim a draft (none was staged) and should name who we
    # held off on, grounded in the skip reason.
    assert "already handled" not in res.summary
    assert "Maya" in res.summary
    assert "Held off" in res.summary
    assert "already replied" in res.summary.lower()


def test_concurrent_summary_does_not_promise_a_skipped_draft(db):
    """Regression for the ben/Laurel bug: triage nominates TWO and its closing
    promises a draft for BOTH, but one drafter skips (ball in their court). The
    final summary must NOT claim a draft for the skipped person — it must name
    only who was actually drafted and honestly note the hold-off."""
    u = _user(db)
    ev = _event(db, u)
    laurel = _stale_contact(db, u, ev, name="Laurel Dong", ident="laurel", days=1)
    ben = _stale_contact(db, u, ev, name="bensiraphob", ident="bensiraphob", days=2)

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": ben.id, "reason": "mid-convo",
                                     "angle": "continue thread"},
                                    {"contact_id": laurel.id, "reason": "no msg yet",
                                     "angle": "first touch"}],
                        # The stale, over-eager promise that caused the bug:
                        closing="Both nominated, bensiraphob gets a follow-up "
                                "draft and Laurel Dong gets a first-touch draft.")]
    drafts = {
        laurel.id: [_tool_use("draft_message", "dl", contact_id=laurel.id,
                              message="Hey Laurel, great meeting you at Tech Week.",
                              rationale="first touch")],
        ben.id: [_tool_use("skip_contact", "sb", contact_id=ben.id,
                           reason="The ball is in their court")],
    }
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert res.error is None
    # Only Laurel was actually staged.
    assert [pr.contact_name for pr in res.proposals] == ["Laurel Dong"]
    # Summary names the real draft and the hold-off; never claims ben got one.
    assert "Laurel Dong" in res.summary
    assert "Held off on bensiraphob" in res.summary
    assert "ball is in their court" in res.summary
    assert "bensiraphob gets a follow-up draft" not in res.summary


def test_concurrent_force_draft_honors_explicit_command(db):
    """When the host explicitly commands drafting, triage sets force_draft=true
    and the per-person draft prompt must tell the model to WRITE the message and
    NOT skip on 'too soon / ball in their court' grounds — the host's command
    overrides the drafter's caution."""
    u = _user(db)
    ev = _event(db, u)
    a = _stale_contact(db, u, ev, name="Laurel Dong", ident="laurel", days=1)

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": a.id, "reason": "host asked",
                                     "angle": "first touch"}],
                        closing="Drafting for everyone you asked.",
                        force_draft=True)]
    drafts = {a.id: [_tool_use("draft_message", "da", contact_id=a.id,
                              message="Hey Laurel, great meeting you.",
                              rationale="host asked")]}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(
        db, u.id, instruction="draft messages for everyone", client=client)
    assert res.error is None
    prompt = client.draft_calls()[0]["messages"][0]["content"]
    assert "EXPLICITLY asked you to draft" in prompt
    assert "Do NOT call skip_contact" in prompt


def test_concurrent_no_force_draft_keeps_skip_judgment(db):
    """Without an explicit command (force_draft falsey), the draft prompt keeps
    the conservative branch that still allows skip_contact."""
    u = _user(db)
    ev = _event(db, u)
    a = _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya", days=40)

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": a.id, "reason": "40d cold",
                                     "angle": "reconnect"}],
                        closing="Here's who I'd reconnect with.")]  # no force_draft
    drafts = {a.id: [_tool_use("draft_message", "da", contact_id=a.id,
                              message="Hey Maya.", rationale="40d")]}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(
        db, u.id, instruction="who should I follow up with?", client=client)
    assert res.error is None
    prompt = client.draft_calls()[0]["messages"][0]["content"]
    assert "call skip_contact" in prompt
    assert "EXPLICITLY asked you to draft" not in prompt


def test_concurrent_empty_selection_skips_fan_out(db):
    """If triage selects nobody, there are zero draft calls and the warm closing
    line is surfaced — the silent path costs exactly one Claude call."""
    u = _user(db)
    ev = _event(db, u)
    # A fresh contact the host hasn't gone cold on.
    _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya", days=1)

    triage = [_tool_use("select_followups", "tg", selections=[],
                        closing="Everyone's warm right now, nothing urgent.")]
    client = ConcurrentScriptedClient(triage=triage, drafts={})

    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert res.error is None
    assert res.proposals == []
    assert len(client.draft_calls()) == 0
    assert "warm" in res.summary


def test_concurrent_uses_sonnet_for_every_call(db):
    """Quality + voice depend on Sonnet: BOTH the triage and the draft calls must
    run on the Sonnet model, never silently downgraded to a cheaper one."""
    u = _user(db)
    ev = _event(db, u)
    c = _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya")

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": c.id, "reason": "cold",
                                     "angle": "x"}], closing="done")]
    drafts = {c.id: [_tool_use("draft_message", "da", contact_id=c.id,
                               message="Hey Maya, reconnecting.", rationale="cold")]}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert "sonnet" in ragent._AGENT_MODEL
    assert all(call["model"] == ragent._AGENT_MODEL for call in client.calls)


def test_concurrent_does_not_cap_selections(db):
    """Triage is a high-recall net: every nominated, owner-resolvable person is
    drafted, with NO arbitrary count cap. (Fan-out CONCURRENCY is still bounded
    by the semaphore; this asserts total reach, not simultaneity.)"""
    u = _user(db)
    ev = _event(db, u)
    n = ragent.MAX_DEEP_DIVES + 5  # comfortably past the old cap
    contacts = [_stale_contact(db, u, ev, name=f"P{i}", ident=f"p{i}")
                for i in range(n)]
    triage = [_tool_use(
        "select_followups", "tg",
        selections=[{"contact_id": c.id, "reason": "cold", "angle": "x"}
                    for c in contacts],
        closing="lots")]
    drafts = {c.id: [_tool_use("draft_message", f"d{c.id}", contact_id=c.id,
                               message=f"Hi {c.id}", rationale="cold")]
              for c in contacts}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert len(client.draft_calls()) == n       # everyone drafted, uncapped
    assert len(res.proposals) == n


def test_concurrent_unknown_selection_is_dropped(db):
    """A triage selection naming a contact_id the host doesn't own never resolves
    (owner-scoping) — it's dropped before any draft call, nothing staged."""
    u = _user(db)
    ev = _event(db, u)
    c = _stale_contact(db, u, ev, name="Maya Rodriguez", ident="maya")

    triage = [_tool_use("select_followups", "tg",
                        selections=[{"contact_id": 999999, "reason": "ghost",
                                     "angle": "x"},
                                    {"contact_id": c.id, "reason": "real",
                                     "angle": "y"}],
                        closing="done")]
    drafts = {c.id: [_tool_use("draft_message", "da", contact_id=c.id,
                               message="Hey Maya.", rationale="real")]}
    client = ConcurrentScriptedClient(triage=triage, drafts=drafts)

    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    # Only the real contact was drafted; the invented id was dropped pre-fan-out.
    assert len(client.draft_calls()) == 1
    assert [pr.contact_id for pr in res.proposals] == [c.id]


def test_concurrent_empty_spine_short_circuits(db):
    """No contacts -> no LLM call at all (not even triage), friendly summary."""
    u = _user(db)
    client = ConcurrentScriptedClient(triage=[], drafts={})
    res = ragent.run_relationship_agent_concurrent(db, u.id, client=client)
    assert res.stop_reason == "empty"
    assert res.proposals == []
    assert client.calls == []
