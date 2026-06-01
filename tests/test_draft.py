"""
Tests for the intent-steered connect-drafting engine (agents/draft.py) and the
POST /api/draft endpoint. No network : the offline template path runs when
ANTHROPIC_API_KEY is unset, and the LLM path is exercised with a stubbed client.
"""
import os

import pytest

from backend.agents import draft as D


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    # Force the deterministic template path by default (no live calls in CI).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_template_intents_differ_and_note_is_short():
    talked = "scaling Postgres without downtime"
    out = {intent: D.draft_connect(
                contact_name="Maya Rodriguez", sender_name="Sam",
                intent=intent, context=talked)
           for intent in ("Sales", "Hiring", "Networking", "Vibes")}
    # Each intent yields a distinct first_message (intent is the lever).
    msgs = {k: v["first_message"] for k, v in out.items()}
    assert len(set(msgs.values())) == 4, msgs
    for v in out.values():
        assert len(v["connection_note"]) <= D.NOTE_MAX
        assert "Maya" in v["first_message"]          # first name, used once
        assert "—" not in v["first_message"]          # no em dashes
        assert "scaling Postgres" in v["first_message"]  # leads with the topic


def test_freeform_intent_is_literal():
    out = D.draft_connect(contact_name="Lee", sender_name="Sam",
                          intent="wants advice on fundraising", context="")
    assert "fundraising" in out["first_message"].lower()


def test_empty_context_stays_generic_no_invention():
    out = D.draft_connect(contact_name="Dana Kim", sender_name="Sam",
                          intent="Networking", context="")
    # Warm but doesn't fabricate a talked-about detail.
    assert "Dana" in out["first_message"]
    assert out["connection_note"]


def test_booking_link_woven_into_followup_but_not_note():
    link = "https://calendly.com/sam/15min"
    out = D.draft_connect(contact_name="Ada", sender_name="Sam",
                          intent="Sales", context="cutting cloud spend",
                          booking_link=link)
    assert link in out["first_message"]
    assert link not in out["connection_note"]
    # Vibes carries no ask, so no link even when one is provided.
    vibes = D.draft_connect(contact_name="Ada", sender_name="Sam",
                            intent="Vibes", context="", booking_link=link)
    assert link not in vibes["first_message"]


def test_llm_path_parses_fenced_json(monkeypatch):
    """With a key present + a stubbed client returning fenced JSON, we parse it
    and still enforce the 200-char note cap."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    long_note = "x" * 400

    class _Block:
        type = "text"
        text = ('```json\n{"connection_note": "%s", '
                '"first_message": "Hey Ada. Good chat."}\n```' % long_note)

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                # System prompt must be cache-flagged; no assistant prefill (would
                # 400 on Sonnet 4.6).
                assert kw["system"][0]["cache_control"]["type"] == "ephemeral"
                assert all(m["role"] != "assistant" for m in kw["messages"])
                return _Resp()

    monkeypatch.setattr(D, "_compose_client", lambda: _Client())
    out = D.draft_connect(contact_name="Ada", sender_name="Sam",
                          intent="Networking", context="boats")
    assert out["first_message"] == "Hey Ada. Good chat."
    assert len(out["connection_note"]) <= D.NOTE_MAX


def test_llm_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def _boom():
        class _C:
            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("network down")
        return _C()

    monkeypatch.setattr(D, "_compose_client", _boom)
    out = D.draft_connect(contact_name="Ada", sender_name="Sam",
                          intent="Hiring", context="rust compilers")
    assert "Ada" in out["first_message"]            # template recovered
    assert out["connection_note"]


def test_endpoint_returns_both_fields_and_defaults_booking_from_user():
    """POST /api/draft returns the two fields and defaults the booking link to
    the signed-in user's saved calendly_url."""
    from fastapi.testclient import TestClient
    from backend.db import reset_db, init_db, SessionLocal
    from backend.main import app
    from backend import models, auth as A

    os.environ["INPERSON_HOSTS"] = "event.surpluslayer.com"
    reset_db(); init_db()
    db = SessionLocal()
    u = models.User(name="Sam Operator", unipile_account_id="acct_d",
                    linkedin_status="active",
                    calendly_url="https://calendly.com/sam/15min")
    db.add(u); db.flush()
    tok = A.create_session(db, u).session_token
    db.commit(); db.close()

    c = TestClient(app, base_url="https://event.surpluslayer.com")
    c.cookies.set("surplus_session", tok)
    r = c.post("/api/draft", json={
        "contact": {"name": "Maya Rodriguez", "headline": "Founder"},
        "intent": "Sales",
        "context": "cutting onboarding time",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connection_note"] and body["first_message"]
    assert len(body["connection_note"]) <= D.NOTE_MAX
    # Saved calendly link surfaced as the next step (Sales carries an ask).
    assert "calendly.com/sam/15min" in body["first_message"]
