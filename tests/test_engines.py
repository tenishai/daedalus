"""The engine seam (tenishai/agents#51 step 1): what the Agent hands an engine, and
the enforcement every engine gets through `call_tool`, whatever runs the loop."""

from daedalus.engines import EngineTurn
from daedalus.engines.litellm import LiteLLMEngine
from daedalus.task import Task
from tests.fakes import FakeTools, RecordingApprover, ScriptedLLM
from tests.test_agent_tools import READ_SPEC, WRITE_SPEC, make_agent


class CapturingEngine:
    """Records the turn it was given and replies without calling a model."""

    name = "capturing"

    def __init__(self):
        self.turn: EngineTurn | None = None

    async def run(self, turn: EngineTurn) -> Task:
        self.turn = turn
        return turn.task.reply("captured")


def _with_engine(agent, engine):
    agent._engine = lambda: engine
    return agent


async def test_the_default_engine_is_litellm(tmp_path):
    agent = make_agent(tmp_path, allow=["files.read"])
    engine = agent._engine()
    assert isinstance(engine, LiteLLMEngine) and engine.llm is agent.llm


async def test_an_engine_gets_only_the_allowlisted_tools(tmp_path):
    engine = CapturingEngine()
    agent = _with_engine(
        make_agent(tmp_path, allow=["files.read"], tools=FakeTools([READ_SPEC, WRITE_SPEC])),
        engine,
    )
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.agent_text() == "captured"
    assert [t["name"] for t in engine.turn.tools] == ["files.read"]
    assert engine.turn.messages[-1]["role"] == "user"
    assert engine.turn.spec is agent.bundle.spec


async def test_call_tool_refuses_a_tool_outside_the_allowlist(tmp_path):
    """An engine that runs its own loop (Claude Code) calls tools by name; the
    allowlist must hold there too, not only in the LiteLLM loop's name map."""
    engine = CapturingEngine()
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent = _with_engine(make_agent(tmp_path, allow=["files.read"], tools=tools), engine)
    await agent.handle_task(Task.from_text("hi"))
    assert await engine.turn.call_tool("files.write", {}) == (
        "error: unknown or disallowed tool 'files.write'"
    )
    assert tools.calls == []
    assert await engine.turn.call_tool("files.read", {"path": "/x"}) == "ok: files.read"
    assert tools.calls == [("files.read", {"path": "/x"})]


async def test_call_tool_applies_approval_gates(tmp_path):
    engine = CapturingEngine()
    approver = RecordingApprover(False)
    tools = FakeTools([WRITE_SPEC])
    agent = _with_engine(
        make_agent(
            tmp_path, allow=["files.write"], tools=tools, approver=approver,
            gates='    - match: "files.write"\n      require: human\n',
        ),
        engine,
    )
    await agent.handle_task(Task.from_text("hi"))
    result = await engine.turn.call_tool("files.write", {"path": "/x"})
    assert result.startswith("blocked: files.write requires approval")
    assert approver.asked and approver.asked[0][0] == "files.write"
    assert tools.calls == []


async def test_the_litellm_engine_runs_a_turn_on_its_own(tmp_path):
    """The engine needs only the turn: no Agent behind it."""
    agent = make_agent(tmp_path, allow=["files.read"])
    seen = []

    async def call_tool(name, arguments):
        seen.append(name)
        return "contents"

    from daedalus.llm import LLMResponse
    from daedalus.protocols import NoBudget, NoCheckpoints, ToolCall

    llm = ScriptedLLM(
        LLMResponse(text="", tool_calls=[ToolCall(id="1", name="files__read", arguments={})]),
        "done",
    )
    events = []
    task = await LiteLLMEngine(llm).run(
        EngineTurn(
            task=Task.from_text("read"),
            messages=[{"role": "user", "content": "read"}],
            tools=[READ_SPEC],
            call_tool=call_tool,
            emit=lambda kind, **f: events.append(kind),
            budget=NoBudget(),
            checkpoints=NoCheckpoints(),
            spec=agent.bundle.spec,
        )
    )
    assert task.agent_text() == "done"
    assert seen == ["files.read"]
    assert events == ["tool_call", "tool_result"]
