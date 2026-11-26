"""A reply cut off by max_tokens (finish_reason "length") on the litellm engine.

Its tool calls never run: each gets an error result telling the model to re-issue it.
A cut-off reply with no tool call follows tools.on_truncated (default: reply, as before).
"""

import copy

from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import ReasonCode, Task, TaskState
from tests.fakes import FakeTools, ScriptedLLM
from tests.test_agent_tools import READ_SPEC, WRITE_SPEC, make_agent


class SnapshotLLM(ScriptedLLM):
    """Keeps a copy of what each call sent (ScriptedLLM keeps the live list)."""

    async def complete(self, messages, tools=None):
        reply = await super().complete(messages, tools=tools)
        self.calls[-1] = copy.deepcopy(messages)
        return reply


def cut(*calls, text=""):
    return LLMResponse(text=text, tool_calls=list(calls), finish_reason="length")


WRITE = ToolCall(id="w1", name="files__write", arguments={"path": "/a", "content": "half"})


async def test_tool_calls_of_a_cut_reply_are_not_run(tmp_path):
    llm = SnapshotLLM(cut(WRITE), "done")
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent = make_agent(tmp_path, allow=["files.write"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("write it"))
    assert tools.calls == []
    assert task.agent_text() == "done" and task.state is not TaskState.FAILED
    sent = llm.calls[-1]
    assert sent[-2]["role"] == "assistant" and sent[-2]["tool_calls"][0]["id"] == "w1"
    assert sent[-1] == {
        "role": "tool",
        "tool_call_id": "w1",
        "content": sent[-1]["content"],
    }
    assert sent[-1]["content"].startswith('Tool call "files.write" was not executed')
    assert "Re-issue the tool call" in sent[-1]["content"]


async def test_a_complete_reply_runs_its_tool_calls_as_before(tmp_path):
    ok = LLMResponse(text="", tool_calls=[WRITE], finish_reason="tool_calls")
    tools = FakeTools([READ_SPEC, WRITE_SPEC], {"files.write": "ok"})
    agent = make_agent(tmp_path, allow=["files.write"], llm=ScriptedLLM(ok, "done"), tools=tools)
    await agent.handle_task(Task.from_text("write it"))
    assert len(tools.calls) == 1


async def test_salvaged_calls_from_a_cut_reply_are_not_run(tmp_path):
    text = '<tool_call>{"name": "files__write", "arguments": {"path": "/a"}}</tool_call>'
    llm = SnapshotLLM(cut(text=text), "done")
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.write"], llm=llm, tools=tools,
        tools_extra="  salvage_text_calls: true\n",
    )
    await agent.handle_task(Task.from_text("write it"))
    assert tools.calls == []
    assert llm.calls[-1][-1]["content"].startswith('Tool call "files.write" was not executed')


async def test_a_cut_reply_without_calls_is_the_answer_by_default(tmp_path):
    agent = make_agent(tmp_path, allow=[], llm=ScriptedLLM(cut(text="partial")))
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is not TaskState.FAILED
    assert task.agent_text().startswith("partial\n\n[reply truncated")


async def test_on_truncated_fail_marks_the_task_failed(tmp_path):
    agent = make_agent(
        tmp_path, allow=[], llm=ScriptedLLM(cut(text="partial")),
        tools_extra="  on_truncated: fail\n",
    )
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.FAILED and task.reason_code is ReasonCode.BUDGET_EXHAUSTED
    assert task.agent_text().startswith("partial\n\n[reply truncated")


async def test_on_truncated_continue_asks_the_model_to_carry_on(tmp_path):
    llm = SnapshotLLM(cut(text="thinking..."), "finished")
    agent = make_agent(tmp_path, allow=[], llm=llm, tools_extra="  on_truncated: continue\n")
    task = await agent.handle_task(Task.from_text("go"))
    assert task.agent_text() == "finished"
    sent = llm.calls[-1]
    assert sent[-2] == {"role": "assistant", "content": "thinking..."}
    assert sent[-1]["role"] == "user" and "cut off by the output token limit" in sent[-1]["content"]


async def test_on_truncated_continue_is_bounded_by_max_steps(tmp_path):
    llm = ScriptedLLM(*[cut(text="more") for _ in range(20)])
    agent = make_agent(tmp_path, allow=[], llm=llm, tools_extra="  on_truncated: continue\n")
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.FAILED and len(llm.calls) == 10
