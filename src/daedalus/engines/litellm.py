"""The LiteLLM engine: daedalus's own agent loop over any model LiteLLM routes.

The loop from Agent._loop and _wrap_up, moved with mechanical substitutions (self._emit
to turn.emit, self.bundle.spec to turn.spec, and so on); tests/test_agent_tools.py is
its behaviour spec. Tool dispatch is split: this loop maps model-safe names back to
canonical ones, and turn.call_tool (Agent._call_tool) enforces the allowlist, approval
gates and provider.
"""

from __future__ import annotations

import json
import re

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


# Only JSON inside these tags is salvaged (#200): a reply that quotes tool-shaped
# JSON in prose ("you asked me to repeat this: {...}") is the model talking, not
# calling. An unclosed tag runs to the end of the reply (a cut-off answer).
_TAGGED = re.compile(r"<(tool_call|tools)>(.*?)(?:</\1>|\Z)", re.S)


def salvage_enabled(spec) -> bool:
    """tools.salvage_text_calls, or on by default only for a self-hosted endpoint."""
    if spec.tools.salvage_text_calls is not None:
        return spec.tools.salvage_text_calls
    return bool(spec.model.api_base)


def salvage_tool_calls(text: str, known: set[str]) -> list[ToolCall]:
    """Recover tool calls that local models emit as text instead of structure.

    (Qwen-Coder on vLLM writes {"name": ..., "arguments": ...} inside <tools>
    tags — mimicking the definition format — so the server parser misses it.)
    Only JSON inside <tool_call> or <tools> tags counts (#200).

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
    tagged = (m.group(2) for m in _TAGGED.finditer(text))
    for obj in (o for span in tagged for o in _json_objects(span)):
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


_STUB_PREFIX = "[earlier result removed to save context: "


# Lines worth keeping when a result is stubbed: the one line that mattered in a long
# build or test log (architect pattern b1df1649ab83d416). Broad on purpose, capped below.
_ERROR_LINE = re.compile(
    r"error|fail|exception|traceback|panic|fatal|denied|refused|timed out|assert"
    r"|segfault|undefined reference|cannot find|not found|warning",
    re.I,
)
_ERROR_LINES_MAX, _ERROR_LINE_CHARS = 12, 300
#: Rough characters per token, for the stop-when-under-target check. The prompt's real
#: size comes back on the next call's usage; this only decides when to stop stubbing.
_CHARS_PER_TOKEN = 4


def _error_lines(content: str) -> list[str]:
    kept = [line.strip()[:_ERROR_LINE_CHARS] for line in content.splitlines()
            if _ERROR_LINE.search(line)]
    return kept[:_ERROR_LINES_MAX]


def _read_key(name: str, arguments: str):
    """(tool, path) for a call that reads a file, else None: the key that makes an
    earlier read of the same file a stale view (architect pattern 0486f0e9971c61be)."""
    if not name.endswith("read"):
        return None
    try:
        path = json.loads(arguments or "{}").get("path")
    except (ValueError, AttributeError):
        return None
    return (name, path) if isinstance(path, str) and path else None


def compact_tool_results(
    messages: list[dict], *, keep_recent: int, min_chars: int, target_chars: int | None = None
) -> tuple:
    """Stub old tool results (tools.compact) until target_chars are removed.

    Candidates are the tool results older than the newest keep_recent, longer than
    min_chars, text, and not already stubbed. Stale file views go first: a read of a
    file that is read again later is the version the model should not rely on. Then
    the rest, oldest first. With target_chars None every candidate is stubbed.

    Each stub keeps its message slot and tool_call_id, names the call and its size so
    the model can make it again, and carries up to 12 error-looking lines from the
    result, so a failing test's message outlives its log. A stubbed message is
    replaced, not edited, so anything else holding the old dict sees no change.
    Returns (count, chars removed).
    """
    calls, order = {}, []
    for message in messages:
        for call in message.get("tool_calls") or ():
            function = call.get("function", {})
            entry = (function.get("name", "?"), function.get("arguments", ""))
            calls[call.get("id")] = entry
            order.append((call.get("id"), _read_key(*entry)))
    later_reads, stale = set(), set()
    for call_id, key in reversed(order):
        if key is not None:
            if key in later_reads:
                stale.add(call_id)
            later_reads.add(key)

    tool_slots = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    candidates = []
    for i in tool_slots[: max(len(tool_slots) - keep_recent, 0)]:
        content = messages[i].get("content")
        if not isinstance(content, str) or len(content) <= min_chars:
            continue
        if content.startswith(_STUB_PREFIX):
            continue
        candidates.append(i)
    candidates.sort(key=lambda i: (messages[i].get("tool_call_id") not in stale, i))

    count = removed = 0
    for i in candidates:
        if target_chars is not None and removed >= target_chars:
            break
        content = messages[i]["content"]
        call_id = messages[i].get("tool_call_id")
        name, arguments = calls.get(call_id, ("?", ""))
        if len(arguments) > 200:
            arguments = arguments[:200] + "..."
        why = " (a later read of the same file replaced it)" if call_id in stale else ""
        stub = (
            f"{_STUB_PREFIX}{name} {arguments} returned {len(content)} characters{why}. "
            "Make the call again if you still need them.]"
        )
        errors = _error_lines(content)
        if errors:
            stub += "\nError-looking lines it contained:\n" + "\n".join(errors)
        messages[i] = {**messages[i], "content": stub}
        count += 1
        removed += len(content) - len(stub)
    return count, removed


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
        by_bare: dict[str, list[str]] = {}
        for tool in allowed:
            by_bare.setdefault(tool["name"].rsplit(".", 1)[-1], []).append(tool["name"])

        def resolve(name: str) -> str | None:
            """Canonical name for a model-returned one. Gemini sometimes drops the
            server prefix (`remember` for `colleagues__remember`, #167); accept that
            only when exactly one offered tool ends in it."""
            if name in by_munged:
                return by_munged[name]
            matches = by_bare.get(name, [])
            return matches[0] if len(matches) == 1 else None

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
                emit("loop_limit", reason="budget")
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
            # One event per model call, budget or not (#268): the run record and
            # log-summed token totals both read it.
            emit("usage", model=getattr(self.llm, "model", None), **response.usage_fields())
            truncated = response.finish_reason == "length"
            if truncated:
                # Silent truncation looks like a complete reply to every
                # surface downstream (finding #27, born as kobo #1/#4) —
                # trace it AND mark the reply so operators see the cut.
                emit("truncated", max_tokens=spec.model.max_tokens)
            budget.spend(response.tokens_spent())
            if budget.remaining() is not None:
                emit(
                    "budget",
                    spent=response.tokens_spent(),
                    remaining=budget.remaining(),
                )
            if response.reasoning:
                emit("reasoning", text=response.reasoning)
            tool_calls = response.tool_calls
            if not tool_calls and llm_tools and salvage_enabled(spec):
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
            if (
                context_limit
                and response.input_tokens is not None
                and response.input_tokens >= context_limit
            ):
                # Stop before the tool results grow the context further (#98).
                emit("context_limit", input_tokens=response.input_tokens)
                exhausted = "context"
                break
            # The same call with the same arguments, over and over, is a model stuck
            # in a loop (#100): each repeat costs a step and returns what it already
            # has. Count consecutive identical calls, across steps and within one.
            streaks = []
            for call in tool_calls:
                key = (resolve(call.name) or call.name, _arguments_key(call.arguments))
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
                # Tier-1 re-check: a hallucinated name misses the map.
                name = resolve(call.name)
                canonical = name or call.name  # trace speaks canonical
                if name is not None and call.name not in by_munged:
                    emit("bare_tool_name", given=call.name, resolved=name)
                emit("tool_call", name=canonical, arguments=call.arguments)
                result = (
                    await turn.call_tool(name, call.arguments)
                    if name is not None
                    else f"error: unknown or disallowed tool {call.name!r}"
                )
                # How the call ended, from the code path (#268). Read before the
                # repeat note: a str `+` drops it.
                outcome = getattr(result, "outcome", None) if name is not None else "error"
                if max_repeats and n > 1:
                    result += _REPEAT_NOTE
                emit("tool_result", name=canonical, content=result,
                     **({"outcome": outcome} if outcome else {}))
                last_attempt = f"{canonical} -> {result[:200]}"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
            # tools.compact: once this prompt reached at_tokens, stub the old results
            # before the next call sends them all again. Unset = never.
            compact = spec.tools.compact
            if (
                compact is not None
                and response.input_tokens is not None
                and response.input_tokens >= compact.at_tokens
            ):
                # Stop stubbing once the prompt is estimated back under target_ratio
                # of at_tokens, so a compaction removes a block at once and then lets
                # the prompt grow again: the cache prefix changes once per compaction,
                # not every step (architect pattern a11f2890707d55bb).
                target_tokens = int(compact.at_tokens * compact.target_ratio)
                over = max(response.input_tokens - target_tokens, 0)
                count, removed = compact_tool_results(
                    messages,
                    keep_recent=compact.keep_recent,
                    min_chars=compact.min_chars,
                    target_chars=over * _CHARS_PER_TOKEN,
                )
                if count:
                    emit(
                        "compact",
                        input_tokens=response.input_tokens,
                        results=count,
                        chars_removed=removed,
                    )

        emit("loop_limit", reason=exhausted)
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
        turn.emit("usage", model=getattr(self.llm, "model", None), **response.usage_fields())
        budget.spend(response.tokens_spent())
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
