"""Unit tests for composer context wiring: compose_reason + infer_intent."""
from __future__ import annotations

from backend.agents.relationship.pipeline.compose.drafting import (
    _natural_action,
    compose_inputs,
    compose_reason,
    infer_intent,
)


def test_compose_reason_prefers_open_next_step_over_triage():
    ctx = {
        "facts": {
            "next_step": "send the pricing deck",
            "latest_update": "Raised Series A",
        },
        "prior": [],
    }
    sel = {"reason": "marked follow-up", "angle": "check in"}
    assert compose_reason(ctx, sel, angle="") == "send the pricing deck"


def test_compose_reason_drops_stale_next_step_when_thread_delivered():
    ctx = {
        "facts": {"next_step": None},  # build_context strips after delivery
        "prior": [{"who": "host", "text": "Here's the pricing deck."}],
    }
    sel = {"reason": "marked follow-up", "angle": "check in"}
    out = compose_reason(ctx, sel, angle="")
    assert "pricing deck" not in out.lower()
    assert out != "send the pricing deck"


def test_compose_reason_merges_host_and_person():
    ctx = {
        "facts": {"next_step": "send the pricing deck"},
        "prior": [],
    }
    sel = {"reason": "marked follow-up"}
    out = compose_reason(
        ctx, sel, angle="",
        directive="follow up with everyone in my network",
    )
    assert "send the pricing deck" in out
    assert "follow up with everyone" in out


def test_compose_reason_host_only_when_no_spine_signal():
    ctx = {"facts": {}, "prior": []}
    out = compose_reason(ctx, {}, "", directive="follow up with everyone")
    assert out == "follow up with everyone"


def test_compose_reason_uses_angle_when_no_facts():
    ctx = {"facts": {}}
    sel = {"reason": "stale 45d", "angle": "propose coffee in SOMA"}
    assert compose_reason(ctx, sel, "propose coffee in SOMA") == "propose coffee in SOMA"


def test_infer_intent_reply_from_thread_question():
    ctx = {
        "prior": [{"who": "them", "text": "Is the dinner Tuesday or Wednesday?"}],
        "facts": {},
    }
    intent = infer_intent(ctx, angle="answer their logistics question")
    assert intent.kind == "reply"


def test_infer_intent_schedule_from_coffee_thread():
    ctx = {
        "prior": [{"who": "them", "text": "Would love to grab coffee next week!"}],
        "facts": {},
    }
    intent = infer_intent(ctx, angle="suggest coffee near their office")
    assert intent.kind == "schedule"
    assert "coffee" in intent.objective.lower()


def test_infer_intent_merges_directive_into_objective():
    ctx = {
        "prior": [{"who": "them", "text": "Would love to grab coffee next week!"}],
        "facts": {},
    }
    directive = "follow up with sales contacts"
    intent = infer_intent(
        ctx, angle="suggest coffee near their office", directive=directive)
    assert intent.kind == "schedule"
    assert "coffee" in intent.objective.lower()
    assert "sales" in intent.objective.lower()


def test_infer_intent_schedule_from_host_directive_when_thread_thin():
    ctx = {"prior": [], "facts": {"met_at": "SaaStr dinner"}}
    intent = infer_intent(
        ctx,
        directive="follow up with sales contacts and book a call",
        angle="propose times for a short sales call",
    )
    assert intent.kind == "schedule"


def test_infer_intent_congratulate_on_update():
    ctx = {
        "prior": [],
        "facts": {"latest_update": "Promoted to VP Engineering"},
    }
    intent = infer_intent(ctx, sel_reason="recent update in the data")
    assert intent.kind == "congratulate"


def test_infer_intent_per_person_schedule_beats_generic_directive():
    """Same host directive, different threads -> different objectives."""
    directive = "follow up with sales"
    call_ctx = {
        "prior": [{"who": "them", "text": "Happy to jump on a quick call this week."}],
        "facts": {},
    }
    coffee_ctx = {
        "prior": [{"who": "them", "text": "Let's grab coffee when you're in SF."}],
        "facts": {},
    }
    call = infer_intent(call_ctx, directive=directive, angle="propose call times")
    coffee = infer_intent(coffee_ctx, directive=directive, angle="suggest coffee spots")
    assert call.kind == "schedule"
    assert coffee.kind == "schedule"
    assert call.objective != coffee.objective


def test_compose_inputs_returns_both_layers():
    ctx = {
        "facts": {"next_step": "send deck"},
        "prior": [],
    }
    reason, intent = compose_inputs(
        ctx, directive="message everyone", sel={"reason": "stale"}, angle="")
    assert "send deck" in reason
    assert "message everyone" in reason
    assert "send deck" in intent.objective
    assert "message everyone" in intent.objective


def test_natural_action_schedule_open_loop():
    ctx = {
        "prior": [
            {"who": "host", "text": "I'll find time for a call next week."},
        ],
        "facts": {},
    }
    na = _natural_action(ctx)
    assert "meet" in na.lower() or "time" in na.lower() or "call" in na.lower()
