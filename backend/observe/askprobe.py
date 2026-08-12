"""backend/observe/askprobe.py : narrate the ask/draft path as it runs.

routes/book.py calls these at real points in a real request; each one
publishes to backend/observe/bus.py, which Observe's /stream/activity
tails. The point is to name the ACTUAL machinery a click sets off --
which model, which cache, which gate, which fallback, which concurrency
limit -- rather than a generic "thinking...".

Two rules this module holds to:

  1. Only name tech that is really in the path. Modal is in this repo
     (modal==1.2.6, dispatched from backend/jobs.py) but it is env-gated
     behind USE_MODAL and is NOT in the ask path, so it is reported with
     its real on/off state instead of implied. There is no vector store,
     no embedding index and no RAG anywhere in this codebase (checked, not
     assumed), so nothing here claims retrieval.
  2. Report what actually happened, not what usually happens. Whether
     selection ran on the model or fell back to keyword routing is
     detected per-call, not guessed.

Every function is wrapped by the caller's own try/except and bus.publish
swallows its own errors: instrumentation must never break a real ask.
"""
from __future__ import annotations

import os

from . import bus

INFO, OK, WARN, ERR, STEP = "info", "ok", "warn", "error", "step"


def _llm_available() -> bool:
    from ..agents.relationship.book import _anthropic_available
    return _anthropic_available()


def _models() -> tuple:
    from ..agents import llm
    return llm.MODEL, llm.JUDGE_MODEL


def _gate_caps() -> tuple:
    from ..agents import rategate
    return rategate._TOTAL, rategate._BG_CAP


def ask_started(user_id: int, query: str) -> None:
    bus.publish(user_id, STEP, "backend.routes.book:ask_stream",
                f"── ask bar: {query!r} ─────────────")
    total, bg = _gate_caps()
    draft_model, select_model = _models()
    if _llm_available():
        bus.publish(user_id, INFO, "backend.agents.llm",
                    f"  models: select={select_model} · draft={draft_model} · "
                    f"prompt cache=ephemeral on the system prefix · "
                    f"rate gate={total} concurrent ({bg} reserved for background)")
    else:
        bus.publish(user_id, WARN, "backend.agents.relationship.book:_anthropic_available",
                    "  no ANTHROPIC_API_KEY -- selection will use deterministic keyword "
                    "routing and drafting the heuristic composer; no model call in this path")
    bus.publish(user_id, INFO, "backend.jobs:use_modal",
                f"  Modal batch dispatch: {'ON' if _modal_on() else 'OFF'} "
                f"(USE_MODAL) -- not used by the ask path either way; "
                f"ask runs in-process on this worker")


def _modal_on() -> bool:
    try:
        from .. import jobs
        return jobs.use_modal()
    except Exception:  # noqa: BLE001
        return (os.environ.get("USE_MODAL") or "").strip().lower() in {"1", "true", "yes"}


def book_loaded(user_id: int, n_book: int, n_orm: int) -> None:
    bus.publish(user_id, INFO, "backend.routes.book:_load_book",
                f"  book loaded: {n_book} scored contacts · {n_orm} durable Contact rows")


def selection_done(user_id: int, query: str, book: list, res: dict, ms: float) -> None:
    """The candidate-selection step: a deterministic pre-rank, then (if a key
    exists) one cheap-model call that picks who to act on."""
    cap = max(20, int(os.environ.get("ASK_BOOK_CAP", "80")))
    n_book = len(book or [])
    _draft_model, select_model = _models()

    if n_book > cap:
        bus.publish(user_id, INFO,
                    "backend.agents.relationship.book:_prioritized_for_ask",
                    f"  pre-rank: {n_book} contacts → top {cap} by "
                    f"_score_health_heuristic (deterministic, no model call) so the "
                    f"selection prompt stays bounded")
    else:
        bus.publish(user_id, INFO,
                    "backend.agents.relationship.book:_prioritized_for_ask",
                    f"  pre-rank: {n_book} contacts ≤ cap {cap}, whole book sent to selection")

    people = (res or {}).get("people") or []
    if _llm_available():
        bus.publish(user_id, OK, "backend.agents.relationship.book:ask_agent",
                    f"  selection: {select_model} chose {len(people)} of "
                    f"{min(n_book, cap)} candidates in {ms:.0f}ms "
                    f"(selection-only -- draft=null enforced, messages composed separately)")
    else:
        bus.publish(user_id, OK,
                    "backend.agents.relationship.book:_ask_agent_heuristic",
                    f"  selection: keyword routing over the scored book chose "
                    f"{len(people)} in {ms:.0f}ms (no model available)")

    for p in people[:8]:
        reason = (p.get("reason") or "").strip()
        bus.publish(user_id, INFO, "backend.agents.relationship.book:ask_agent",
                    f"    {(p.get('name') or '?')[:28]:<28} {reason[:70]}")


def drafting_started(user_id: int, n_stream: int, n_heuristic: int, inline_cap: int) -> None:
    draft_model, _select = _models()
    total, _bg = _gate_caps()
    bus.publish(user_id, STEP,
                "backend.agents.relationship.pipeline.compose.drafting",
                f"  drafting {n_stream + n_heuristic} of the selected "
                f"(ASK_INLINE_DRAFTS={inline_cap}) · ThreadPoolExecutor(max_workers=6) "
                f"· rate gate caps {total} concurrent model calls")
    if n_stream:
        bus.publish(user_id, INFO,
                    "backend.agents.relationship.pipeline.compose.drafting:stream_from_context",
                    f"    {n_stream} via {draft_model}, streamed token-by-token, with the "
                    f"host's voice profile and that contact's real prior thread")
    if n_heuristic:
        bus.publish(user_id, INFO,
                    "backend.agents.relationship.book:draft_message_cached",
                    f"    {n_heuristic} via the heuristic composer (no durable Contact row "
                    f"to load a thread from), cached per trigger")


def ask_done(user_id: int, n_people: int, ms: float) -> None:
    bus.publish(user_id, OK, "backend.routes.book:ask_stream",
                f"  ask complete: {n_people} people in {ms:.0f}ms "
                f"(streamed to the product over SSE)")


# ── the Draft tap (routes/book.py:/draft) ────────────────────────────────

def draft_tap(user_id: int, name: str, engine: str, channel: str,
              trigger: str, ms: float, chars: int) -> None:
    """One Draft tap. `engine` is which composer actually produced it --
    "shared" (the LLM composer with voice + thread) or "heuristic"."""
    draft_model, _select = _models()
    if engine == "shared":
        bus.publish(user_id, OK,
                    "backend.agents.relationship.pipeline.compose.drafting:compose_followup",
                    f"── draft tap: {name} · {draft_model} · channel={channel} · "
                    f"{chars} chars in {ms:.0f}ms · trigger={trigger[:48]!r}")
    else:
        bus.publish(user_id, OK,
                    "backend.agents.relationship.book:draft_message_cached",
                    f"── draft tap: {name} · heuristic composer (deterministic, no model "
                    f"call) · channel={channel} · {chars} chars in {ms:.0f}ms")
