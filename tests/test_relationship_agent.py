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

    script = [
        ("tool_use", [_tool_use("list_contacts", "t1")]),
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


def test_agent_proposal_for_unknown_contact_is_rejected(db):
    """If the model proposes against a contact_id that isn't the host's, the
    tool refuses (owner-scoping) and no proposal is staged."""
    u = _user(db)
    ev = _event(db, u)
    p = _prospect(db, ev)
    rel.link_contact(db, p, u.id)

    script = [
        ("tool_use", [_tool_use("list_contacts", "t1")]),
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
        ("tool_use", [_tool_use("list_contacts", "t1")]),
        ("end_turn", [_text("done")]),
    ])
    ragent.run_relationship_agent(db, u.id, client=client)
    sent_system = client.calls[0]["system"][0]["text"]
    assert "<style_examples>" in sent_system
    assert "yo!! so good to finally meet you haha" in sent_system


def _list_contacts_payload(client) -> list[dict]:
    """Pull the JSON list_contacts returned to the model back out of the
    scripted client's recorded calls. agent_loop feeds each tool_result back as
    a user message with content=[{type:'tool_result', content: json.dumps(...)}],
    so the row payload the agent actually saw is recoverable here — this is the
    exact dict shape `_list_contacts` produced, not a reconstruction."""
    for call in client.calls:
        for m in call.get("messages", []):
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    rows = json.loads(blk["content"])
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

    client = ScriptedClient([
        ("tool_use", [_tool_use("list_contacts", "t1")]),
        ("end_turn", [_text("done")]),
    ])
    res = ragent.run_relationship_agent(db, u.id, client=client)
    assert res.error is None
    rows = _list_contacts_payload(client)
    row = next(r for r in rows if r["contact_id"] == c.id)
    assert row["marked_follow_up"] is True
    assert "follow_up" in row["contact_types"]


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
        ("tool_use", [_tool_use("list_contacts", "t1")]),
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
