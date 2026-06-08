"""
agents/agent_loop.py : the multi-turn tool-use primitive.

Every other "agent" in this codebase is one-shot : build a prompt, call
`messages.create` ONCE, parse the reply. That's fine for classify/compose,
but it can't *investigate* : look something up, read the result, decide what
to look at next, then act. This module adds the missing piece : a bounded
loop that lets the model call tools, feeds the results back, and repeats
until the model is done (or a step cap is hit).

Design:
- Generic. Knows nothing about relationships/events. Callers pass a system
  prompt, a list of Anthropic tool schemas, and a dict of {tool_name: impl}.
- Bounded. `max_steps` is a hard ceiling : the loop can never spin forever,
  even if the model keeps asking for tools. This is the safety valve that a
  one-shot call gives you for free and a loop does not.
- Fail-soft. A tool that raises returns its error to the model as a
  tool_result (so the model can recover / try another path) rather than
  crashing the run. A transport failure ends the loop with `error` set.
- Auditable. Every tool call + result is recorded on the returned AgentRun
  so the caller (and tests) can see exactly what the agent did.

The loop has NO opinion about side effects : whether a tool actually sends a
message or just stages a proposal is entirely the impl's business. Keep the
"act" tools behind whatever policy gate the caller wants (see
relationship_agent.py, which runs propose-only).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Sonnet is the cheapest current model confirmed to drive tool-use loops well
# in this codebase (llm.py uses it for web_search discovery). Override per
# deployment via RELATIONSHIP_AGENT_MODEL / AGENT_LOOP_MODEL.
DEFAULT_MODEL = os.environ.get("AGENT_LOOP_MODEL", "claude-sonnet-4-6")


@dataclass
class ToolCall:
    """One tool invocation inside the loop, with its result."""
    step: int
    name: str
    input: dict
    result: Any
    error: Optional[str] = None


@dataclass
class AgentRun:
    """The full record of one agent loop : what it did and how it ended.

    `stop_reason` is the model's own stop_reason on the final turn
    ("end_turn" = the model decided it was done) OR "max_steps" if we cut it
    off. `error` is set only on a transport/SDK failure that ended the loop
    early. `final_text` is the model's last natural-language message (its
    summary / sign-off), if any.
    """
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    stop_reason: str = ""
    steps: int = 0
    error: Optional[str] = None

    def calls_named(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]


def _block_type(block: Any) -> str:
    return getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else "")


def _text_of(block: Any) -> str:
    return getattr(block, "text", None) or (block.get("text", "") if isinstance(block, dict) else "")


def _serialize_block(block: Any) -> dict:
    """Normalize one assistant content block to a clean dict for echoing back.

    We can't hand the raw SDK block objects back into the next messages.create
    call : the Anthropic SDK (0.85) re-serializes them with by_alias and chokes
    on None-valued optional fields ("by_alias: NoneType cannot be converted to
    PyBool"). Rebuilding only the fields the API needs sidesteps that entirely
    and keeps the thread minimal.
    """
    t = _block_type(block)
    if t == "text":
        return {"type": "text", "text": _text_of(block)}
    if t == "tool_use":
        name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else "")
        bid = getattr(block, "id", None) or (block.get("id") if isinstance(block, dict) else "")
        binput = getattr(block, "input", None)
        if binput is None and isinstance(block, dict):
            binput = block.get("input", {})
        return {"type": "tool_use", "id": bid, "name": name, "input": dict(binput or {})}
    # Unknown/other block types (e.g. thinking) : pass through if already a dict,
    # else best-effort model_dump, else drop to a text stub.
    if isinstance(block, dict):
        return block
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:  # noqa: BLE001
            pass
    return {"type": "text", "text": _text_of(block)}


def _mark_thread_cache(messages: list[dict]) -> None:
    """Place ONE cache breakpoint at the end of the latest message.

    Each agent step re-sends the whole growing transcript (the tool results,
    especially get_contact payloads, balloon it). Marking the last block as an
    ephemeral cache breakpoint means the entire prefix up to here — system +
    tools + every earlier turn — is read from cache on the NEXT step instead of
    reprocessed, cutting time-to-first-token on every step after the first.

    We keep exactly one breakpoint in the thread (strip any prior one first) so
    that, with the system block's own breakpoint, we stay well under Anthropic's
    4-breakpoint limit. Anthropic auto-reads the longest matching cached prefix,
    so a single moving breakpoint at the tail is enough for incremental caching.
    """
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    last = messages[-1]
    c = last.get("content")
    if isinstance(c, str):
        # Normalise a plain-string message to block form so it can carry a
        # cache_control marker (the initial user_prompt is a bare string).
        last["content"] = [{
            "type": "text", "text": c,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(c, list) and c and isinstance(c[-1], dict):
        c[-1]["cache_control"] = {"type": "ephemeral"}


def run_agent(
    *,
    system: str,
    tools: list[dict],
    tool_impls: dict[str, Callable[..., Any]],
    user_prompt: str,
    model: Optional[str] = None,
    max_steps: int = 8,
    max_tokens: int = 2048,
    client: Any = None,
) -> AgentRun:
    """Run a bounded tool-use loop and return a full transcript.

    Args:
        system : the system prompt (cached on the first turn).
        tools : Anthropic tool schemas (name/description/input_schema).
        tool_impls : {tool_name: callable}. Called with the model's tool
            input as kwargs; whatever it returns is JSON-encoded back to the
            model as the tool_result. Raise to surface an error to the model.
        user_prompt : the kickoff message.
        max_steps : hard ceiling on model turns (the loop's safety valve).
        client : an Anthropic-compatible client (injected for tests). When
            None, a real Anthropic() client is constructed.

    The loop NEVER decides side effects : a tool impl that sends is the impl's
    choice. This function just routes calls and feeds results back.
    """
    run = AgentRun()

    if client is None:
        try:
            from anthropic import Anthropic
            # Reuse llm._api_key(): it strips the trailing newline Railway's
            # dashboard appends to env vars, which otherwise makes httpx reject
            # every request with LocalProtocolError ("Illegal header value").
            # max_retries=2 absorbs a single 429/5xx blip mid-loop.
            from .llm import _api_key
            key = _api_key()
            if not key:
                run.error = "ANTHROPIC_API_KEY not set"
                run.stop_reason = "no_client"
                return run
            client = Anthropic(api_key=key, max_retries=2)
        except Exception as exc:  # noqa: BLE001 : no SDK -> fail soft
            run.error = f"{type(exc).__name__}: {exc}"
            run.stop_reason = "no_client"
            return run

    mdl = model or DEFAULT_MODEL
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    for step in range(1, max_steps + 1):
        run.steps = step
        # Cache the whole transcript prefix so each step after the first reads
        # the prior turns from cache instead of reprocessing the (growing) thread.
        _mark_thread_cache(messages)
        try:
            resp = client.messages.create(
                model=mdl,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=tools,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 : transport/SDK failure ends loop
            run.error = f"{type(exc).__name__}: {exc}"
            run.stop_reason = "error"
            return run

        content = list(getattr(resp, "content", None) or [])
        # Echo the assistant turn back as clean dicts so the next call has the
        # full thread. We rebuild the blocks rather than pass the raw SDK
        # objects, which fail re-serialization (see _serialize_block).
        messages.append({
            "role": "assistant",
            "content": [_serialize_block(b) for b in content] or [{"type": "text", "text": ""}],
        })

        # Capture any natural-language text the model emitted this turn.
        text = "\n".join(_text_of(b) for b in content if _block_type(b) == "text").strip()
        if text:
            run.final_text = text

        tool_uses = [b for b in content if _block_type(b) == "tool_use"]
        stop_reason = getattr(resp, "stop_reason", "") or ""

        if not tool_uses or stop_reason != "tool_use":
            # The model is done : no more tools requested.
            run.stop_reason = stop_reason or "end_turn"
            return run

        # Dispatch every requested tool and feed the results back as one
        # user turn (Anthropic requires all tool_results in a single message).
        tool_results = []
        for tu in tool_uses:
            name = getattr(tu, "name", None) or (tu.get("name") if isinstance(tu, dict) else "")
            tu_id = getattr(tu, "id", None) or (tu.get("id") if isinstance(tu, dict) else "")
            tu_input = getattr(tu, "input", None)
            if tu_input is None and isinstance(tu, dict):
                tu_input = tu.get("input", {})
            tu_input = dict(tu_input or {})

            impl = tool_impls.get(name)
            err = None
            if impl is None:
                result = {"error": f"unknown tool: {name}"}
                err = result["error"]
            else:
                try:
                    result = impl(**tu_input)
                except Exception as exc:  # noqa: BLE001 : surface to model, keep looping
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                    err = result["error"]

            run.tool_calls.append(
                ToolCall(step=step, name=name, input=tu_input, result=result, error=err))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    # Fell out of the loop : hit the step ceiling with the model still asking.
    run.stop_reason = "max_steps"
    return run
