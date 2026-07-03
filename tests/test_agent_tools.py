"""The tool loop and both approval tiers — including the adversarial paths.

House rule from the design doc: hallucinated tool names, ceiling-widen
attempts, and gate denials must all fail closed, as tool results the model
can see — never as crashes.
"""

from pathlib import Path

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.protocols import LLMResponse, ToolCall
from daedalus.task import ReasonCode, Task, TaskState
from tests.fakes import FakeTools, RecordingApprover, ScriptedLLM

GOLDEN = Path(__file__).parent / "golden" / "greeter"

READ_SPEC = {"name": "files.read", "description": "read a file", "parameters": {}}
WRITE_SPEC = {"name": "files.write", "description": "write a file", "parameters": {}}


def tool_turn(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(text="", tool_calls=list(calls))


def make_agent(tmp_path, *, allow, gates="", llm=None, tools=None, approver=None) -> Agent:
    allow_yaml = "".join(f"    - {p}\n" for p in allow)
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: none\n  allow:\n" + (allow_yaml or "    []\n")
        + (f"approvals:\n  gates:\n{gates}" if gates else "")
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return Agent(
        Bundle.load(tmp_path),
        llm=llm or ScriptedLLM("done"),
        tools=tools or FakeTools([READ_SPEC, WRITE_SPEC]),
        approver=approver,
    )


async def test_tool_call_executes_and_result_feeds_back():
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__read", arguments={"path": "/x"})),
        "the file says hi",
    )
    tools = FakeTools([READ_SPEC], {"files.read": "contents: hi"})
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        agent = make_agent(Path(d), allow=["files.read"], llm=llm, tools=tools)
        task = await agent.handle_task(Task.from_text("read /x"))

    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "the file says hi"
    assert tools.calls == [("files.read", {"path": "/x"})]
    tool_message = llm.calls[1][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"] == "contents: hi"


async def test_ceiling_filters_listing_so_model_never_sees_denied_tools(tmp_path):
    llm = ScriptedLLM("done")
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm)
    await agent.handle_task(Task.from_text("hi"))
    listed = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert listed == ["files__read"]  # files.write exists but is not shown


async def test_empty_allowlist_denies_everything(tmp_path):
    llm = ScriptedLLM("done")
    agent = make_agent(tmp_path, allow=[], llm=llm)
    await agent.handle_task(Task.from_text("hi"))
    assert llm.tools_seen[0] is None  # no tools offered at all


async def test_hallucinated_tool_name_fails_closed_as_a_tool_result(tmp_path):
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__delete_everything", arguments={})),
        "understood",
    )
    tools = FakeTools([READ_SPEC])
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == []  # nothing executed
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]


async def test_call_outside_ceiling_is_blocked_even_if_model_forges_the_name(tmp_path):
    # files.write exists in the provider, but the allowlist only has files.read
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__write", arguments={"path": "/pwn"})),
        "ok",
    )
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    await agent.handle_task(Task.from_text("go"))
    assert tools.calls == []
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]


async def test_gate_denied_by_default_denyall(tmp_path):
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__write", arguments={"path": "/a"})),
        "could not write",
    )
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    gates = "    - match: 'files.write*'\n      require: plan_approval\n"
    agent = make_agent(tmp_path, allow=["files.*"], gates=gates, llm=llm, tools=tools)
    await agent.handle_task(Task.from_text("write it"))
    assert tools.calls == []  # gate held with no approver wired
    assert "requires approval" in llm.calls[1][-1]["content"]


async def test_gate_approved_executes_and_approver_saw_the_plan(tmp_path):
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__write", arguments={"path": "/a"})),
        "written",
    )
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    approver = RecordingApprover(True)
    gates = "    - match: 'files.write*'\n      require: plan_approval\n"
    agent = make_agent(
        tmp_path, allow=["files.*"], gates=gates, llm=llm, tools=tools, approver=approver
    )
    task = await agent.handle_task(Task.from_text("write it"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == [("files.write", {"path": "/a"})]
    action, plan = approver.asked[0]
    assert action == "files.write"
    assert plan["arguments"] == {"path": "/a"}


async def test_ungated_tool_needs_no_approval(tmp_path):
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__read", arguments={})), "done"
    )
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    approver = RecordingApprover(False)  # would deny — must never be asked
    gates = "    - match: 'files.write*'\n      require: plan_approval\n"
    agent = make_agent(
        tmp_path, allow=["files.*"], gates=gates, llm=llm, tools=tools, approver=approver
    )
    await agent.handle_task(Task.from_text("read"))
    assert tools.calls == [("files.read", {})]
    assert approver.asked == []


async def test_runaway_tool_loop_hits_max_steps_and_fails_with_reason(tmp_path):
    forever = [
        tool_turn(ToolCall(id=str(i), name="files__read", arguments={})) for i in range(99)
    ]
    llm = ScriptedLLM(*forever)
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]))
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert len(llm.calls) == 10  # ToolsSpec.max_steps default


async def test_provider_exception_becomes_a_tool_result_not_a_crash(tmp_path):
    class ExplodingTools(FakeTools):
        async def call(self, name: str, payload: dict) -> str:
            raise RuntimeError("connection reset")

    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__read", arguments={})), "sorry"
    )
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=ExplodingTools([READ_SPEC])
    )
    task = await agent.handle_task(Task.from_text("read"))
    assert task.state is TaskState.COMPLETED
    assert "tool error: connection reset" in llm.calls[1][-1]["content"]
