"""tools.retry_malformed: a reply whose tool calls can't run is asked again off the
record, so the history the model reads keeps only the reply that was used.

Off by default: a bundle without it sends exactly what it sent before.
"""

import copy

import pytest

from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import Task
from tests.fakes import FakeTools, ScriptedLLM
from tests.test_agent_tools import READ_SPEC, make_agent

GREP_SPEC = {
    "name": "files.search",
    "description": "search",
    "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
}


class SnapshotLLM(ScriptedLLM):
    """Keeps a copy of what each call sent (ScriptedLLM keeps the live list)."""

    async def complete(self, messages, tools=None):
        reply = await super().complete(messages, tools=tools)
        self.calls[-1] = copy.deepcopy(messages)
        return reply


def call(name, arguments, cid="c1", input_tokens=100):
    return LLMResponse(
        text="", tool_calls=[ToolCall(id=cid, name=name, arguments=arguments)],
        input_tokens=input_tokens, output_tokens=10,
    )


GOOD = call("files__read", {"path": "/x"}, cid="good")
UNKNOWN = call("files__edit", {"path": "/x"}, cid="bad")
UNPARSED = call("files__read", {"_raw": '{"path": "/x'}, cid="bad")
MISSING = call("files__search", {}, cid="bad")


async def run(tmp_path, extra, *turns, model_extra="", events=None):
    llm = SnapshotLLM(*turns, "done")
    tools = FakeTools([READ_SPEC, GREP_SPEC], {"files.read": "hi", "files.search": "x:1"})
    agent = make_agent(
        tmp_path, allow=["files.read", "files.search"], llm=llm, tools=tools,
        tools_extra=extra, model_extra=model_extra,
    )
    if events is not None:
        agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("go"))
    return llm, tools, task


def tool_ids(sent):
    return [m.get("tool_call_id") for m in sent if m["role"] == "tool"]


async def test_off_by_default_the_bad_call_stays_in_history(tmp_path):
    llm, tools, task = await run(tmp_path, "", UNKNOWN, GOOD)
    assert task.agent_text() == "done"
    assert len(llm.calls) == 3
    last = llm.calls[-1]
    assert tool_ids(last) == ["bad", "good"]
    assert "unknown or disallowed tool" in [m for m in last if m["role"] == "tool"][0]["content"]


@pytest.mark.parametrize("bad", [UNKNOWN, UNPARSED, MISSING], ids=["unknown", "unparsed",
                                                                     "missing"])
async def test_a_malformed_reply_is_retried_off_the_record(tmp_path, bad):
    llm, tools, task = await run(tmp_path, "  retry_malformed: 2\n", bad, GOOD)
    assert task.agent_text() == "done"
    retry_view = llm.calls[1]
    assert retry_view[-1]["role"] == "tool" and retry_view[-1]["content"].startswith("error: ")
    assert tool_ids(llm.calls[-1]) == ["good"]  # the failed attempt never reached history
    assert len(tools.calls) == 1  # the malformed call never ran


async def test_the_retry_says_what_was_wrong(tmp_path):
    llm, _, _ = await run(tmp_path, "  retry_malformed: 1\n", UNKNOWN, GOOD)
    note = llm.calls[1][-1]["content"]
    assert "unknown tool 'files__edit'" in note and "files__read" in note


async def test_retries_spent_fall_back_to_todays_behaviour(tmp_path):
    llm, tools, task = await run(tmp_path, "  retry_malformed: 2\n", UNKNOWN, UNKNOWN, UNKNOWN)
    assert task.agent_text() == "done"
    assert len(llm.calls) == 4  # first try, two retries, then the answer
    assert tool_ids(llm.calls[-1]) == ["bad"]  # the last reply is kept, nothing is lost


async def test_retries_are_traced_and_charged_to_the_budget(tmp_path):
    events = []
    await run(
        tmp_path, "  retry_malformed: 2\n", UNKNOWN, GOOD,
        model_extra="  budget:\n    per_task_tokens: 100000\n", events=events,
    )
    retries = [e for e in events if e["type"] == "malformed_retry"]
    assert len(retries) == 1 and retries[0]["attempt"] == 1
    spent = [e["spent"] for e in events if e["type"] == "budget"]
    assert len(spent) == 3  # the first reply, the retry and the final answer


async def test_a_good_sibling_is_not_run_when_another_call_is_malformed(tmp_path):
    both = LLMResponse(
        text="",
        tool_calls=[
            ToolCall(id="ok", name="files__read", arguments={"path": "/x"}),
            ToolCall(id="bad", name="files__edit", arguments={}),
        ],
        input_tokens=100,
    )
    llm, tools, _ = await run(tmp_path, "  retry_malformed: 1\n", both, GOOD)
    assert len(tools.calls) == 1  # only the retry's good call ran
    assert "another call in this reply was malformed" in llm.calls[1][-2]["content"]


def test_retry_malformed_is_bounded(tmp_path):
    from daedalus.bundle import BundleError

    with pytest.raises((BundleError, ValueError)):
        make_agent(tmp_path, allow=["files.read"], tools_extra="  retry_malformed: 6\n")
