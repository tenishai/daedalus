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
    tools = FakeTools([READ_SPEC], {"files.read": "the same thing again"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert len(llm.calls) == 10  # ToolsSpec.max_steps default
    # finding #9: the failure says what was last tried, not just that it stopped
    assert "last attempt: files.read -> the same thing again" in task.agent_text()


async def test_event_stream_traces_the_whole_tool_turn(tmp_path):
    """Finding #9: the on_event seam — cli --verbose today, kaminos SSE later."""
    llm = ScriptedLLM(
        LLMResponse(
            text="",
            reasoning="I should read the file",
            tool_calls=[ToolCall(id="1", name="files__read", arguments={"path": "/x"})],
        ),
        "done",
    )
    events: list[dict] = []
    tools = FakeTools([READ_SPEC], {"files.read": "contents"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("read /x"))
    kinds = [e["type"] for e in events]
    assert kinds == ["tools_offered", "reasoning", "tool_call", "tool_result"]
    assert events[0] == {"type": "tools_offered", "names": ["files.read"]}
    assert events[2] == {"type": "tool_call", "name": "files.read", "arguments": {"path": "/x"}}
    assert events[3]["content"] == "contents"


async def test_salvaged_calls_emit_a_salvage_event(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK), "done")
    events: list[dict] = []
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC])
    )
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("go"))
    assert {"type": "salvage", "count": 1} in events


QWEN_LEAK = '<tools>\n{"name": "files__read", "arguments": {"path": "/x"}}\n</tools>'


def test_salvage_recovers_qwen_style_text_tool_call():
    from daedalus.agent import salvage_tool_calls

    calls = salvage_tool_calls(QWEN_LEAK, {"files__read"})
    assert len(calls) == 1
    assert calls[0].name == "files__read"
    assert calls[0].arguments == {"path": "/x"}


def test_salvage_ignores_prose_malformed_json_and_non_toolish_names():
    from daedalus.agent import salvage_tool_calls

    known = {"files__read"}
    assert salvage_tool_calls("use {braces} casually {in prose", known) == []
    assert salvage_tool_calls('{"name": "files__read", "arguments": "not-a-dict"}', known) == []
    assert salvage_tool_calls('{"name": "files__read"', known) == []  # truncated
    # a plain name in ordinary JSON is prose, not a tool call
    assert salvage_tool_calls('{"name": "Bob", "arguments": {}}', known) == []


def test_salvage_surfaces_unknown_toolish_names_for_feedback_not_execution():
    """Finding #8: a hallucinated git__git_ls_files blob must become 'unknown
    tool' feedback the model can correct from, not the user's final answer.
    Execution stays gated on the offered map — see the end-to-end test below."""
    from daedalus.agent import salvage_tool_calls

    calls = salvage_tool_calls('{"name": "git__git_ls_files", "arguments": {}}', {"files__read"})
    assert len(calls) == 1
    assert calls[0].name == "git__git_ls_files"


def test_salvage_dedupes_repeats_and_caps():
    from daedalus.agent import _SALVAGE_CAP, salvage_tool_calls

    repeated = '\n<|im_start|>\n'.join(
        '{"name": "git__git_ls_files", "arguments": {"pattern": "Makefile"}}' for _ in range(15)
    )
    assert len(salvage_tool_calls(repeated, set())) == 1  # 15 identical -> 1

    distinct = "\n".join(f'{{"name": "t__{i}", "arguments": {{}}}}' for i in range(10))
    assert len(salvage_tool_calls(distinct, set())) == _SALVAGE_CAP


async def test_text_leaked_tool_call_is_salvaged_and_executed(tmp_path):
    """Dogfood finding #6: Qwen-Coder emits calls as <tools> text; the runtime
    recognizes (not guesses) them — the name must match an offered tool."""
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK), "the repo is clean")
    tools = FakeTools([READ_SPEC], {"files.read": "ok"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("read /x"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == [("files.read", {"path": "/x"})]
    assert task.agent_text() == "the repo is clean"


async def test_degenerate_repeat_blob_becomes_one_feedback_then_recovery(tmp_path):
    """The full finding-#8 transcript shape: 15 repeated hallucinated calls +
    special tokens -> ONE 'unknown tool' feedback -> model recovers."""
    spew = "sorry\n" + "\n<|im_start|>\n".join(
        '{"name": "git__git_ls_files", "arguments": {"pattern": "Makefile"}}' for _ in range(15)
    )
    llm = ScriptedLLM(LLMResponse(text=spew), "recovered: used the right tool")
    tools = FakeTools([READ_SPEC])
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == []  # hallucinated tool never executed
    second_call = llm.calls[1]
    tool_results = [m for m in second_call if m["role"] == "tool"]
    assert len(tool_results) == 1  # deduped: one feedback, not fifteen
    assert "unknown or disallowed" in tool_results[0]["content"]
    assert task.agent_text() == "recovered: used the right tool"


async def test_salvage_never_fires_when_no_tools_were_offered(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK))
    tools = FakeTools([READ_SPEC])
    agent = make_agent(tmp_path, allow=[], llm=llm, tools=tools)  # empty ceiling
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == []
    assert "<tools>" in task.agent_text()  # passed through as plain text


async def test_builtin_files_wires_from_agent_yaml_alone(tmp_path):
    """provider: none + builtin files must yield working read tools (finding #10)."""
    (tmp_path / "notes.txt").write_text("bundle-visible content")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: none\n"
        f"  builtin:\n    files:\n      roots: ['{tmp_path}']\n"
        "  allow: ['files.*']\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    call = ToolCall(id="1", name="files__read", arguments={"path": str(tmp_path / "notes.txt")})
    llm = ScriptedLLM(tool_turn(call), "read it")
    agent = Agent(Bundle.load(tmp_path), llm=llm)
    task = await agent.handle_task(Task.from_text("read notes"))
    assert task.state is TaskState.COMPLETED
    assert "bundle-visible content" in llm.calls[1][-1]["content"]


async def test_aclose_reaches_closable_providers(tmp_path):
    class ClosableTools(FakeTools):
        closed = False

        async def aclose(self):
            self.closed = True

    tools = ClosableTools([READ_SPEC])
    agent = make_agent(tmp_path, allow=["files.read"], tools=tools)
    await agent.aclose()
    assert tools.closed


def test_unknown_builtin_group_fails_at_load(tmp_path):
    from daedalus.bundle import BundleError

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  builtin:\n    filez: {roots: ['/tmp']}\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    import pytest

    with pytest.raises(BundleError, match="filez"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM())


async def test_mcp_worker_boot_failure_surfaces_as_mcp_error():
    """Finding #13: session lifecycle lives in a worker task. Without the mcp
    package the worker's boot failure must surface as a clean MCPError."""
    import importlib.util

    import pytest

    from daedalus.tools.mcp import MCPError, MCPToolProvider

    if importlib.util.find_spec("mcp") is not None:
        pytest.skip("mcp is installed; boot cannot fail this way")
    provider = MCPToolProvider([{"name": "git", "command": ["echo", "hi"]}])
    with pytest.raises(MCPError, match=r"daedalus\[mcp\]"):
        await provider.tools()
    await provider.aclose()  # idempotent, no worker left behind
    await provider.aclose()


def test_provider_mcp_without_extra_fails_at_load_not_mid_conversation(tmp_path):
    """Dogfood finding #5: a missing extra must be a startup error with the
    install command in it — not an 'error:' line on your first greeting."""
    import importlib.util

    import pytest

    from daedalus.bundle import BundleError

    if importlib.util.find_spec("mcp") is not None:
        pytest.skip("mcp is installed in this env; load cannot fail")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: mcp\n  servers:\n    - name: git\n      command: [echo]\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    with pytest.raises(BundleError, match=r"daedalus\[mcp\]"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM())


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
