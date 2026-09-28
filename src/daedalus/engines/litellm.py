"""The LiteLLM engine: daedalus's own agent loop over any model LiteLLM routes.

The loop from Agent._loop and _wrap_up, moved with mechanical substitutions (self._emit
to turn.emit, self.bundle.spec to turn.spec, and so on); tests/test_agent_tools.py is
its behaviour spec. Tool dispatch is split: this loop maps model-safe names back to
canonical ones, and turn.call_tool (Agent._call_tool) enforces the allowlist, approval
gates and provider.
"""

from __future__ import annotations

import json

from daedalus.engines.base import EngineTurn
from daedalus.protocols import LLM, ToolCall
from daedalus.task import ReasonCode, Task, TaskState


def _munge(name: str) -> str:
    """Canonical `server.tool` -> LLM-safe function name (models dislike dots)."""
    return name.replace(".", "__")


def _strip_image_parts(messages: list[dict]) -> list[dict]:
    """Checkpoint copies of the LLM messages drop image data URIs -- replay
    fidelity for images is not a promise this system makes (same trade the
    journal already made), and a 4-image turn would otherwise re-serialize
    ~100 MB per step."""
    stripped = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            stripped.append({**message, "content": text})
        else:
            stripped.append(message)
    return stripped


_SALVAGE_CAP = 4


def _json_objects(text: str):
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            yield obj
        index = start + max(consumed, 1)


def salvage_tool_calls(text: str, known: set[str]) -> list[ToolCall]:
    """Recover tool calls that local models emit as text instead of structure.

    (Qwen-Coder on vLLM writes {"name": ..., "arguments": ...} inside <tools>
    tags — mimicking the definition format — so the server parser misses it.)

    Known names get executed. Unknown-but-tool-shaped names (containing '__'
    or '.') are salvaged too so the loop answers with 'unknown tool' feedback
    the model can correct from, instead of dumping the blob on the user —
    execution is still hard-gated on the offered-tool map. Plain prose,
    malformed JSON, and non-toolish names pass through as text. Duplicates
    are collapsed (degenerating models repeat themselves) and at most
    _SALVAGE_CAP calls are taken per reply.
    """
    calls: list[ToolCall] = []
    seen: set[tuple] = set()
    for obj in _json_objects(text):
        name, args = obj.get("name"), obj.get("arguments")
        if not isinstance(name, str) or not isinstance(args, dict):
            continue
        if name not in known and "__" not in name and "." not in name:
            continue
        key = (name, json.dumps(args, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(id=f"salvaged-{len(calls)}", name=name, arguments=args))
        if len(calls) == _SALVAGE_CAP:
            return calls
    return calls


# Appended to a repeated call's result when tools.max_repeats is set (#100).
_REPEAT_NOTE = (
    "\n\n[You already made this exact call just before this one. "
    "Use what you have, or try something different.]"
)


def _arguments_key(arguments: dict) -> str:
    """Canonical form of a call's arguments, so key order doesn't hide a repeat."""
    return json.dumps(arguments, sort_keys=True, default=str)


def _to_openai(spec: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": _munge(spec["name"]),
            "description": spec.get("description", ""),
            "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def _assistant_message(text: str, tool_calls: list[ToolCall]) -> dict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in tool_calls
        ],
    }


class LiteLLMEngine:
    """Calls the model once per step and runs the tool calls it asks for."""

    name = "litellm"

    def __init__(self, llm: LLM):
        self.llm = llm

    async def run(self, turn: EngineTurn) -> Task:
        task, messages, allowed = turn.task, turn.messages, turn.tools
        spec, emit = turn.spec, turn.emit
        llm_tools = [_to_openai(tool) for tool in allowed] or None
        by_munged = {_munge(tool["name"]): tool["name"] for tool in allowed}
        budget = turn.budget

        last_attempt = ""
        context_limit = spec.tools.context_tokens
        max_repeats = spec.tools.max_repeats
        last_key, streak, repeated = None, 0, ""
        exhausted = "steps"
        for _ in range(spec.tools.max_steps):
            await turn.checkpoints.save(
                task.id,
                {
                    "task": task.model_dump(exclude={"history": {"__all__": {"images"}}}),
                    "messages": _strip_image_parts(messages),
                },
            )
            if budget.remaining() == 0:
                # checkpoint intentionally kept: re-fund and resume()
                # The reply is read by HUMANS on every surface (a Slack
                # channel got the raw sentinel once, as an agent's entire
                # answer); machines key on state/reason, never this text.
                return task.reply(
                    "I had to stop before finishing -- this task ran past "
                    f"my token budget ({spec.model.budget.per_task_tokens}). "
                    "A narrower ask may fit; my operator can raise the limit.",
                    state=TaskState.FAILED,
                    reason=ReasonCode.BUDGET_EXHAUSTED,
                )
            response = await self.llm.complete(messages, tools=llm_tools)
            truncated = response.finish_reason == "length"
            if truncated:
                # Silent truncation looks like a complete reply to every
                # surface downstream (finding #27, born as kobo #1/#4) —
                # trace it AND mark the reply so operators see the cut.
                emit("truncated", max_tokens=spec.model.max_tokens)
            budget.spend(response.input_tokens + response.output_tokens)
            if budget.remaining() is not None:
                emit(
                    "budget",
                    spent=response.input_tokens + response.output_tokens,
                    remaining=budget.remaining(),
                )
            if response.reasoning:
                emit("reasoning", text=response.reasoning)
            tool_calls = response.tool_calls
            if not tool_calls and llm_tools:
                tool_calls = salvage_tool_calls(response.text, set(by_munged))
                if tool_calls:
                    emit("salvage", count=len(tool_calls))
            if not tool_calls:
                await turn.checkpoints.drop(task.id)
                text = response.text
                if truncated:
                    text += (
                        f"\n\n[reply truncated: hit the model's max_tokens "
                        f"limit ({spec.model.max_tokens})]"
                    )
                return task.reply(text)
            if context_limit and response.input_tokens >= context_limit:
                # Stop before the tool results grow the context further (#98).
                emit("context_limit", input_tokens=response.input_tokens)
                exhausted = "context"
                break
            # The same call with the same arguments, over and over, is a model stuck
            # in a loop (#100): each repeat costs a step and returns what it already
            # has. Count consecutive identical calls, across steps and within one.
            streaks = []
            for call in tool_calls:
                key = (by_munged.get(call.name, call.name), _arguments_key(call.arguments))
                streak = streak + 1 if key == last_key else 1
                last_key = key
                streaks.append(streak)
                if max_repeats and streak >= max_repeats and not repeated:
                    repeated = key[0]
            if repeated:
                emit("repeat_limit", name=repeated, count=max_repeats)
                exhausted = "repeats"
                break

            messages.append(_assistant_message(response.text, tool_calls))
            for call, n in zip(tool_calls, streaks, strict=True):
                canonical = by_munged.get(call.name, call.name)  # trace speaks canonical
                emit("tool_call", name=canonical, arguments=call.arguments)
                # Tier-1 re-check: a hallucinated name misses the map.
                name = by_munged.get(call.name)
                result = (
                    await turn.call_tool(name, call.arguments)
                    if name is not None
                    else f"error: unknown or disallowed tool {call.name!r}"
                )
                if max_repeats and n > 1:
                    result += _REPEAT_NOTE
                emit("tool_result", name=canonical, content=result)
                last_attempt = f"{canonical} -> {result[:200]}"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        detail = (
            f" The last thing I tried: {last_attempt}." if last_attempt else ""
        )
        if exhausted == "repeats":
            why = (
                f"new things to try (I made the same {repeated} call "
                f"{max_repeats} times in a row)"
            )
            reason = ReasonCode.POLICY_VIOLATION
            stopped = (
                "I had to stop before finishing -- I made the same "
                f"{repeated} call {max_repeats} times in a row.{detail} A narrower "
                "ask may fit; my operator can raise the limit."
            )
        elif exhausted == "context":
            why = f"room in my context ({context_limit} tokens)"
            reason = ReasonCode.BUDGET_EXHAUSTED
            stopped = (
                "I had to stop before finishing -- this task reached my context "
                f"limit of {context_limit} tokens.{detail} A narrower ask may fit; "
                "my operator can raise the limit."
            )
        else:
            why = f"tool steps ({spec.tools.max_steps})"
            reason = ReasonCode.POLICY_VIOLATION
            stopped = (
                "I had to stop before finishing -- this task needed more than "
                f"the {spec.tools.max_steps} tool steps I'm allowed "
                f"in one go.{detail} A narrower ask may fit; my operator can "
                "raise the limit."
            )
        # A spent per_task_tokens budget is a hard stop: the wrap-up is one more model
        # call, so it only runs while budget remains (NoBudget reports None).
        if spec.tools.on_exhausted == "wrap_up" and budget.remaining() != 0:
            partial = await self._wrap_up(turn, messages, why, budget)
            if partial:
                return task.reply(partial, state=TaskState.FAILED, reason=reason)
        return task.reply(stopped, state=TaskState.FAILED, reason=reason)

    async def _wrap_up(
        self, turn: EngineTurn, messages: list[dict], why: str, budget
    ) -> str | None:
        """One last call with no tools offered: the best answer from what the loop
        already found, marked partial (#98). None when it fails or says nothing,
        so the caller falls back to the plain stop message."""
        from daedalus.llm import LLMError

        ask = (
            f"You have run out of {why} for this task, so this is your last turn and "
            "you cannot call any tools. Using only what you have already found, give "
            "your best answer now, and say plainly what you did not get to."
        )
        try:
            response = await self.llm.complete(
                [*messages, {"role": "user", "content": ask}], tools=None
            )
        except LLMError as exc:
            turn.emit("wrap_up", ok=False, error=str(exc)[:200])
            return None
        budget.spend(response.input_tokens + response.output_tokens)
        if response.reasoning:
            turn.emit("reasoning", text=response.reasoning)
        text = response.text.strip()
        turn.emit("wrap_up", ok=bool(text))
        if not text:
            return None
        if response.finish_reason == "length":
            # Same as the loop's own final reply (finding #27): a cut-off answer must
            # not read as complete, least of all at a small model's context ceiling.
            turn.emit("truncated", max_tokens=turn.spec.model.max_tokens)
            text += (
                f"\n\n[reply truncated: hit the model's max_tokens limit "
                f"({turn.spec.model.max_tokens})]"
            )
        return f"{text}\n\n[partial answer: I ran out of {why} before finishing]"
