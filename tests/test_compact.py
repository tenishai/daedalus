"""tools.compact: the litellm loop stubs old tool results once the prompt grows.

Off by default: a bundle without the block must send exactly what it sent before.
"""

import copy

import pytest

from daedalus.engines.litellm import compact_tool_results
from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import Task
from tests.fakes import FakeTools, ScriptedLLM
from tests.test_agent_tools import READ_SPEC, make_agent

BIG = "x" * 5000


class SnapshotLLM(ScriptedLLM):
    """ScriptedLLM keeps a reference to the live message list; this keeps a copy of
    what each call actually sent."""

    async def complete(self, messages, tools=None):
        reply = await super().complete(messages, tools=tools)
        self.calls[-1] = copy.deepcopy(messages)
        return reply


def read_turn(i, input_tokens):
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(id=f"c{i}", name="files__read", arguments={"path": f"/f{i}"})],
        input_tokens=input_tokens,
    )


def tool_messages(sent):
    return [m for m in sent if m["role"] == "tool"]


async def run(tmp_path, tools_extra, *turns, result=BIG, events=None):
    llm = SnapshotLLM(*turns, "done")
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": result}), tools_extra=tools_extra,
    )
    if events is not None:
        agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("read them all"))
    return llm, task


async def test_without_the_block_every_result_is_resent_whole(tmp_path):
    llm, task = await run(tmp_path, "", *(read_turn(i, 900_000) for i in range(6)))
    assert task.agent_text() == "done"
    last = tool_messages(llm.calls[-1])
    assert len(last) == 6 and all(m["content"] == BIG for m in last)


async def test_old_results_are_stubbed_once_the_prompt_reaches_at_tokens(tmp_path):
    extra = "  compact:\n    at_tokens: 1000\n    keep_recent: 2\n"
    llm, task = await run(tmp_path, extra, *(read_turn(i, 1000) for i in range(5)))
    assert task.agent_text() == "done"
    last = tool_messages(llm.calls[-1])
    assert [m["tool_call_id"] for m in last] == [f"c{i}" for i in range(5)]
    assert [m["content"] for m in last[-2:]] == [BIG, BIG]
    for i, m in enumerate(last[:-2]):
        assert m["content"].startswith("[earlier result removed to save context: files__read")
        assert f'"/f{i}"' in m["content"] and "5000 characters" in m["content"]


async def test_nothing_is_stubbed_below_at_tokens(tmp_path):
    extra = "  compact:\n    at_tokens: 1000\n    keep_recent: 1\n"
    llm, _ = await run(tmp_path, extra, *(read_turn(i, 999) for i in range(4)))
    assert all(m["content"] == BIG for m in tool_messages(llm.calls[-1]))


async def test_short_results_are_kept_whole(tmp_path):
    extra = "  compact:\n    at_tokens: 10\n    keep_recent: 1\n    min_chars: 100\n"
    llm, _ = await run(tmp_path, extra, *(read_turn(i, 50) for i in range(4)), result="short")
    assert all(m["content"] == "short" for m in tool_messages(llm.calls[-1]))


async def test_a_compaction_is_traced(tmp_path):
    events = []
    extra = "  compact:\n    at_tokens: 1000\n    keep_recent: 1\n"
    await run(tmp_path, extra, *(read_turn(i, 2000) for i in range(3)), events=events)
    compacts = [e for e in events if e["type"] == "compact"]
    assert compacts and compacts[0]["input_tokens"] == 2000
    assert compacts[0]["results"] == 1 and compacts[0]["chars_removed"] > 4000


def test_the_threshold_must_sit_below_context_tokens(tmp_path):
    from daedalus.bundle import BundleError

    with pytest.raises((BundleError, ValueError)):
        make_agent(
            tmp_path, allow=["files.read"],
            tools_extra="  context_tokens: 1000\n  compact:\n    at_tokens: 1000\n",
        )


def test_compact_replaces_messages_and_is_idempotent():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "files__read", "arguments": '{"path": "/a"}'}},
            {"id": "b", "type": "function",
             "function": {"name": "files__read", "arguments": '{"path": "/b"}'}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": BIG},
        {"role": "tool", "tool_call_id": "b", "content": BIG},
    ]
    original = messages[2]
    assert compact_tool_results(messages, keep_recent=1, min_chars=1000)[0] == 1
    assert original["content"] == BIG  # the old dict is untouched; the slot was replaced
    assert messages[2]["tool_call_id"] == "a" and messages[3]["content"] == BIG
    assert compact_tool_results(messages, keep_recent=1, min_chars=0) == (0, 0)
