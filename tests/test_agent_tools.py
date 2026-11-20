"""The tool loop and both approval tiers — including the adversarial paths.

House rule from the design doc: hallucinated tool names, ceiling-widen
attempts, and gate denials must all fail closed, as tool results the model
can see — never as crashes.
"""

from pathlib import Path

import pytest

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


def make_agent(
    tmp_path, *, allow, gates="", llm=None, tools=None, approver=None, tools_extra="",
    model_extra="",
) -> Agent:
    allow_yaml = "".join(f"    - {p}\n" for p in allow)
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n" + model_extra +
        "tools:\n  provider: none\n" + tools_extra + "  allow:\n" + (allow_yaml or "    []\n")
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


COLLEAGUE_SPEC = {"name": "colleagues.remember", "description": "keep a fact", "parameters": {}}
NOTES_READ_SPEC = {"name": "notes.read", "description": "read a note", "parameters": {}}


async def test_bare_tool_name_resolves_when_exactly_one_tool_ends_in_it(tmp_path):
    """Gemini drops the server prefix (#167): `remember` -> colleagues.remember."""
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="remember", arguments={"fact": "x"})),
        "kept",
    )
    tools = FakeTools([READ_SPEC, COLLEAGUE_SPEC])
    agent = make_agent(tmp_path, allow=["files.read", "colleagues.*"], llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("go"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == [("colleagues.remember", {"fact": "x"})]
    shortened = {"type": "bare_tool_name", "given": "remember", "resolved": "colleagues.remember"}
    assert shortened in events


async def test_ambiguous_bare_tool_name_is_refused(tmp_path):
    llm = ScriptedLLM(tool_turn(ToolCall(id="1", name="read", arguments={})), "ok")
    tools = FakeTools([READ_SPEC, NOTES_READ_SPEC])
    agent = make_agent(tmp_path, allow=["files.read", "notes.read"], llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("go"))
    assert tools.calls == []
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]
    assert not any(e["type"] == "bare_tool_name" for e in events)


async def test_unknown_bare_tool_name_is_refused(tmp_path):
    llm = ScriptedLLM(tool_turn(ToolCall(id="1", name="delete", arguments={})), "ok")
    tools = FakeTools([READ_SPEC, COLLEAGUE_SPEC])
    agent = make_agent(tmp_path, allow=["files.read", "colleagues.*"], llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("go"))
    assert tools.calls == []
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]
    assert not any(e["type"] == "bare_tool_name" for e in events)


async def test_the_repeat_guard_counts_a_bare_name_as_its_tool(tmp_path):
    """A model alternating `remember` and `colleagues__remember` repeats one call."""
    calls = [
        tool_turn(ToolCall(id=str(i), name=n, arguments={"fact": "x"}))
        for i, n in enumerate(["remember", "colleagues__remember"] * 3)
    ]
    llm = ScriptedLLM(*calls)
    tools = FakeTools([COLLEAGUE_SPEC])
    events: list[dict] = []
    agent = make_agent(
        tmp_path, allow=["colleagues.*"], llm=llm, tools=tools, tools_extra="  max_repeats: 3\n"
    )
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.state is TaskState.FAILED
    assert {"type": "repeat_limit", "name": "colleagues.remember", "count": 3} in events


async def test_bare_name_of_a_tool_outside_the_allowlist_is_refused(tmp_path):
    # files.write exists in the provider but is not allowed, so it is never offered
    llm = ScriptedLLM(tool_turn(ToolCall(id="1", name="write", arguments={"path": "/p"})), "ok")
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    await agent.handle_task(Task.from_text("go"))
    assert tools.calls == []
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


async def test_gate_with_no_approver_withholds_the_tool_and_says_why(tmp_path):
    """No approver wired (DenyAll): a gated tool is not offered at all, and the model
    is told why, instead of being offered it and refused on every call (the retry
    loop architect pattern 3f170e7aff8d918f warns about). Still fail-closed: a call
    the model makes anyway (e.g. text-leaked) finds no tool."""
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="files__write", arguments={"path": "/a"})),
        "could not write",
    )
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    gates = "    - match: 'files.write*'\n      require: plan_approval\n"
    agent = make_agent(tmp_path, allow=["files.*"], gates=gates, llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("write it"))
    assert tools.calls == []  # gate held with no approver wired
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["files__read"]
    system = llm.calls[0][0]["content"]
    assert "Tools you can't use in this run" in system and "files.write" in system
    assert {"type": "tools_withheld", "names": ["files.write"]} in events
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]


async def test_a_wired_approver_keeps_gated_tools_offered(tmp_path):
    llm = ScriptedLLM("done")
    gates = "    - match: 'files.write*'\n      require: plan_approval\n"
    agent = make_agent(
        tmp_path, allow=["files.*"], gates=gates, llm=llm,
        tools=FakeTools([READ_SPEC, WRITE_SPEC]), approver=RecordingApprover(True),
    )
    await agent.handle_task(Task.from_text("hi"))
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["files__read", "files__write"]
    assert "Tools you can't use" not in llm.calls[0][0]["content"]


def test_the_withheld_note_is_added_once():
    from daedalus.agent import _with_withheld_note

    messages = [{"role": "system", "content": "persona"}, {"role": "user", "content": "x"}]
    once = _with_withheld_note(messages, {"files.write"})
    twice = _with_withheld_note(once, {"files.write"})
    assert twice == once
    assert once[0]["content"].count("Tools you can't use in this run") == 1
    assert messages[0]["content"] == "persona"  # the caller's list is not mutated


async def test_a_wildcard_gate_with_no_approver_withholds_every_tool(tmp_path):
    """#268: a read-only agent can keep every tool behind a `*` gate. With no approver
    wired (DenyAll), the pattern is matched by fnmatch and every tool is withheld,
    not offered and refused."""
    llm = ScriptedLLM("nothing to use")
    tools = FakeTools([READ_SPEC, WRITE_SPEC])
    gates = "    - match: '*'\n      require: plan_approval\n"
    agent = make_agent(tmp_path, allow=["files.*"], gates=gates, llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("hi"))
    assert llm.tools_seen[0] is None  # nothing offered
    assert {"type": "tools_withheld", "names": ["files.read", "files.write"]} in events


async def test_a_wired_approver_makes_every_gate_approvable(tmp_path):
    """#268, documented in docs/arch/approvals.md: the approver is per agent, not
    per gate. Once one is wired, a `*` gate withholds nothing; each call asks."""
    llm = ScriptedLLM("done")
    gates = "    - match: '*'\n      require: plan_approval\n"
    agent = make_agent(
        tmp_path, allow=["files.*"], gates=gates, llm=llm,
        tools=FakeTools([READ_SPEC, WRITE_SPEC]), approver=RecordingApprover(False),
    )
    await agent.handle_task(Task.from_text("hi"))
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["files__read", "files__write"]


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
    assert "The last thing I tried: files.read -> the same thing again" in task.agent_text()


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
    # A usage event follows every model call (#268).
    assert kinds == [
        "tools_offered", "usage", "reasoning", "tool_call", "tool_result", "usage"
    ]
    assert events[0] == {"type": "tools_offered", "names": ["files.read"]}
    assert events[3] == {"type": "tool_call", "name": "files.read", "arguments": {"path": "/x"}}
    assert events[4]["content"] == "contents"


async def test_salvaged_calls_emit_a_salvage_event(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK), "done")
    events: list[dict] = []
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra=SALVAGE_ON,
    )
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("go"))
    assert {"type": "salvage", "count": 1} in events


QWEN_LEAK = '<tools>\n{"name": "files__read", "arguments": {"path": "/x"}}\n</tools>'
SALVAGE_ON = "  salvage_text_calls: true\n"


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

    calls = salvage_tool_calls(
        '<tool_call>{"name": "git__git_ls_files", "arguments": {}}</tool_call>', {"files__read"}
    )
    assert len(calls) == 1
    assert calls[0].name == "git__git_ls_files"


def test_salvage_dedupes_repeats_and_caps():
    from daedalus.agent import _SALVAGE_CAP, salvage_tool_calls

    repeated = "<tools>" + '\n<|im_start|>\n'.join(
        '{"name": "git__git_ls_files", "arguments": {"pattern": "Makefile"}}' for _ in range(15)
    ) + "</tools>"
    assert len(salvage_tool_calls(repeated, set())) == 1  # 15 identical -> 1

    distinct = "<tools>" + "\n".join(
        f'{{"name": "t__{i}", "arguments": {{}}}}' for i in range(10)
    ) + "</tools>"
    assert len(salvage_tool_calls(distinct, set())) == _SALVAGE_CAP


async def test_text_leaked_tool_call_is_salvaged_and_executed(tmp_path):
    """Dogfood finding #6: Qwen-Coder emits calls as <tools> text; the runtime
    recognizes (not guesses) them — the name must match an offered tool."""
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK), "the repo is clean")
    tools = FakeTools([READ_SPEC], {"files.read": "ok"})
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools, tools_extra=SALVAGE_ON
    )
    task = await agent.handle_task(Task.from_text("read /x"))
    assert task.state is TaskState.COMPLETED
    assert tools.calls == [("files.read", {"path": "/x"})]
    assert task.agent_text() == "the repo is clean"


async def test_degenerate_repeat_blob_becomes_one_feedback_then_recovery(tmp_path):
    """The full finding-#8 transcript shape: 15 repeated hallucinated calls +
    special tokens -> ONE 'unknown tool' feedback -> model recovers."""
    spew = "sorry\n<tools>" + "\n<|im_start|>\n".join(
        '{"name": "git__git_ls_files", "arguments": {"pattern": "Makefile"}}' for _ in range(15)
    )  # unclosed: the reply was cut off mid-blob
    llm = ScriptedLLM(LLMResponse(text=spew), "recovered: used the right tool")
    tools = FakeTools([READ_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools, tools_extra=SALVAGE_ON
    )
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
    agent = make_agent(  # empty ceiling
        tmp_path, allow=[], llm=llm, tools=tools, tools_extra=SALVAGE_ON
    )
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


async def test_provider_down_is_a_task_failure_not_an_exception(tmp_path):
    """Finding #19: liminal down -> unhandled ConnectError -> a2a 500 ->
    kaminos 500 -> browser JSON.parse error. Layer 1: the agent turns a
    dead provider into a FAILED task with the reason in the reply."""

    class DeadGateway:
        async def tools(self):
            raise ConnectionError("connection refused :9100")

        async def call(self, name, payload):
            raise AssertionError("never reached")

    events: list[dict] = []
    agent = make_agent(tmp_path, allow=["files.*"], tools=DeadGateway())
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.ESCALATION
    assert "tool provider unavailable" in task.agent_text()
    assert "connection refused :9100" in task.agent_text()
    assert any(e["type"] == "tool_provider_error" for e in events)


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


# Wrap-up (#98): when steps or context run out, one tool-less call for the best
# partial answer. State and reason are unchanged from "stop", so callers keying on
# them see no difference; only the text improves.


class FailingLastLLM(ScriptedLLM):
    """Scripted, but the call with no tools offered raises, as a server might."""

    async def complete(self, messages, tools=None):
        if tools is None:
            from daedalus.llm import LLMError

            self.calls.append(messages)
            raise LLMError("m: insufficient memory")
        return await super().complete(messages, tools=tools)


def _loop_forever(n=10):
    return [tool_turn(ToolCall(id=str(i), name="files__read", arguments={})) for i in range(n)]


async def test_wrap_up_answers_from_what_was_found_when_steps_run_out(tmp_path):
    llm = ScriptedLLM(*_loop_forever(), "Here is what I found so far.")
    tools = FakeTools([READ_SPEC], {"files.read": "a fact"})
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert len(llm.calls) == 11 and llm.tools_seen[-1] is None
    assert "you cannot call any tools" in llm.calls[-1][-1]["content"]
    assert llm.calls[-1][-2]["role"] == "tool"  # it saw the last tool result
    assert task.agent_text() == (
        "Here is what I found so far.\n\n"
        "[partial answer: I ran out of tool steps (10) before finishing]"
    )


@pytest.mark.parametrize("last", ["", "   "])
async def test_an_empty_wrap_up_falls_back_to_the_stop_message(tmp_path, last):
    llm = ScriptedLLM(*_loop_forever(), last)
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.agent_text().startswith("I had to stop before finishing -- this task needed more")


async def test_a_failed_wrap_up_falls_back_to_the_stop_message(tmp_path):
    llm = FailingLastLLM(*_loop_forever())
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert len(llm.calls) == 11
    assert task.agent_text().startswith("I had to stop before finishing -- this task needed more")


async def test_context_limit_stops_before_running_the_tools(tmp_path):
    big = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="1", name="files__read", arguments={})],
        input_tokens=5000,
    )
    llm = ScriptedLLM(big)
    tools = FakeTools([READ_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  context_tokens: 4000\n",
    )
    task = await agent.handle_task(Task.from_text("big"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.BUDGET_EXHAUSTED
    assert len(llm.calls) == 1 and tools.calls == []
    assert "reached my context limit of 4000 tokens" in task.agent_text()


async def test_context_limit_with_wrap_up(tmp_path):
    small = LLMResponse(
        text="", tool_calls=[ToolCall(id="1", name="files__read", arguments={})],
        input_tokens=1000,
    )
    big = LLMResponse(
        text="", tool_calls=[ToolCall(id="2", name="files__read", arguments={})],
        input_tokens=4000,
    )
    llm = ScriptedLLM(small, big, "Partial review.")
    tools = FakeTools([READ_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  context_tokens: 4000\n  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("big"))
    assert task.reason_code is ReasonCode.BUDGET_EXHAUSTED
    assert len(tools.calls) == 1  # the first call's tool ran; the second's did not
    assert llm.tools_seen[-1] is None
    assert task.agent_text() == (
        "Partial review.\n\n"
        "[partial answer: I ran out of room in my context (4000 tokens) before finishing]"
    )


def test_context_tokens_must_be_positive(tmp_path):
    from daedalus.bundle import BundleError

    with pytest.raises((BundleError, ValueError)):
        make_agent(tmp_path, allow=["files.read"], tools_extra="  context_tokens: 0\n")


async def test_wrap_up_respects_a_spent_token_budget(tmp_path):
    # Prudence, PR 99 r1: with per_task_tokens spent by the last step, the wrap-up
    # made one more call. A spent budget is a hard stop; the plain message stands.
    spend_it = LLMResponse(
        text="", tool_calls=[ToolCall(id="1", name="files__read", arguments={})],
        input_tokens=900, output_tokens=200,
    )
    llm = ScriptedLLM(spend_it, "should never be asked")
    (tmp_path / "b").mkdir()
    agent = make_agent(
        tmp_path / "b", allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  max_steps: 1\n  on_exhausted: wrap_up\n",
    )
    agent.bundle.spec.model.budget.per_task_tokens = 1000
    task = await agent.handle_task(Task.from_text("loop"))
    assert len(llm.calls) == 1
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert task.agent_text().startswith("I had to stop before finishing")


async def test_a_truncated_wrap_up_says_so(tmp_path):
    cut = LLMResponse(text="Half a review", finish_reason="length")
    llm = ScriptedLLM(*_loop_forever(), cut)
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    text = task.agent_text()
    assert "[reply truncated: hit the model's max_tokens limit" in text
    assert text.endswith("[partial answer: I ran out of tool steps (10) before finishing]")


# tools.max_repeats (#100): the same call with the same arguments, N times in a row,
# ends the loop like running out of steps. Measured on a 9B reviewing techne PR 1029:
# 29 identical files.read calls in a row used up the step budget.


def _read(i, **arguments):
    return tool_turn(ToolCall(id=str(i), name="files__read", arguments=arguments))


async def test_repeated_call_stops_the_loop_at_max_repeats(tmp_path):
    llm = ScriptedLLM(*[_read(i, path="/x") for i in range(10)])
    tools = FakeTools([READ_SPEC], {"files.read": "a fact"})
    events: list[dict] = []
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  max_repeats: 3\n",
    )
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert len(llm.calls) == 3
    assert len(tools.calls) == 2  # the third identical call is never run
    assert task.agent_text().startswith(
        "I had to stop before finishing -- I made the same files.read call 3 times in a row."
    )
    assert {"type": "repeat_limit", "name": "files.read", "count": 3} in events


async def test_a_repeat_gets_a_note_and_the_first_call_does_not(tmp_path):
    llm = ScriptedLLM(_read(1, path="/x"), _read(2, path="/x"), "done")
    tools = FakeTools([READ_SPEC], {"files.read": "a fact"})
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  max_repeats: 3\n",
    )
    task = await agent.handle_task(Task.from_text("read"))
    assert task.agent_text() == "done"
    results = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"]
    assert results[0] == "a fact"
    assert results[1].startswith("a fact\n\n[You already made this exact call")


async def test_different_arguments_never_count_as_a_repeat(tmp_path):
    llm = ScriptedLLM(*[_read(i, path=f"/{i % 2}") for i in range(10)])
    tools = FakeTools([READ_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  max_repeats: 2\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert len(llm.calls) == 10  # ran out of steps, not repeats
    assert "needed more than the 10 tool steps" in task.agent_text()
    assert all("[You already made" not in str(m.get("content") or "") for m in llm.calls[-1])


async def test_argument_order_does_not_hide_a_repeat(tmp_path):
    llm = ScriptedLLM(
        _read(1, path="/x", limit=10), _read(2, limit=10, path="/x"), "unreached"
    )
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  max_repeats: 2\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert len(llm.calls) == 2
    assert "same files.read call 2 times in a row" in task.agent_text()


async def test_identical_calls_within_one_step_count(tmp_path):
    twice = LLMResponse(text="", tool_calls=[
        ToolCall(id="1", name="files__read", arguments={"path": "/x"}),
        ToolCall(id="2", name="files__read", arguments={"path": "/x"}),
    ])
    llm = ScriptedLLM(twice, "unreached")
    tools = FakeTools([READ_SPEC])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=tools,
        tools_extra="  max_repeats: 2\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert len(llm.calls) == 1 and tools.calls == []
    assert task.reason_code is ReasonCode.POLICY_VIOLATION


async def test_repeat_limit_with_wrap_up(tmp_path):
    llm = ScriptedLLM(*[_read(i, path="/x") for i in range(3)], "What I found.")
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
        tools_extra="  max_repeats: 3\n  on_exhausted: wrap_up\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert llm.tools_seen[-1] is None
    assert task.agent_text() == (
        "What I found.\n\n[partial answer: I ran out of new things to try "
        "(I made the same files.read call 3 times in a row) before finishing]"
    )


async def test_repeats_are_not_limited_or_noted_by_default(tmp_path):
    llm = ScriptedLLM(*[_read(i, path="/x") for i in range(10)])
    agent = make_agent(
        tmp_path, allow=["files.read"], llm=llm, tools=FakeTools([READ_SPEC]),
    )
    await agent.handle_task(Task.from_text("loop"))
    assert len(llm.calls) == 10
    assert all("[You already made" not in str(m.get("content") or "") for m in llm.calls[-1])


@pytest.mark.parametrize("value", ["1", "0"])
def test_max_repeats_below_two_is_rejected(tmp_path, value):
    from daedalus.bundle import BundleError

    with pytest.raises((BundleError, ValueError)):
        make_agent(tmp_path, allow=["files.read"], tools_extra=f"  max_repeats: {value}\n")


# --- #149: an agent answering a colleague can't ask onward --------------------

ASK_SPEC = {"name": "colleagues.ask_colleague", "description": "ask", "parameters": {}}


async def test_answering_a_colleague_withholds_ask_colleague(tmp_path):
    """Depth >= 1 means another agent asked. Offering ask_colleague then is how
    A -> B -> A happens; withholding it makes the bounce impossible."""
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="colleagues__ask_colleague", arguments={})),
        "I can't reach that from here.",
    )
    # knowledge_only, so this test keeps pinning the ask withholding alone;
    # the knowledge-only filter has its own tests below (kobo#310).
    tools = FakeTools([READ_SPEC, ASK_SPEC], knowledge_only=True)
    agent = make_agent(tmp_path, allow=["files.read", "colleagues.*"], llm=llm, tools=tools)
    events: list[dict] = []
    agent.on_event = events.append
    task = Task.from_text("what is monitor 42?")
    task.metadata["cabal_delegation_depth"] = 1
    await agent.handle_task(task)
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["files__read"]
    assert "You are answering a colleague" in llm.calls[0][0]["content"]
    assert {"type": "colleague_ask_withheld_inbound"} in events
    assert tools.calls == []
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]


async def test_a_depth_zero_caller_keeps_ask_colleague(tmp_path):
    """A caller that sends no depth (a person via the switchboard) is not a colleague."""
    llm = ScriptedLLM("done")
    tools = FakeTools([READ_SPEC, ASK_SPEC])
    agent = make_agent(tmp_path, allow=["files.read", "colleagues.*"], llm=llm, tools=tools)
    await agent.handle_task(Task.from_text("hi"))
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert "colleagues__ask_colleague" in offered
    assert "You are answering a colleague" not in llm.calls[0][0]["content"]


class _MemoryCheckpoints:
    def __init__(self):
        self.states: dict[str, dict] = {}

    async def save(self, task_id, state):
        self.states[task_id] = state

    async def load(self, task_id):
        return self.states.get(task_id)

    async def drop(self, task_id):
        self.states.pop(task_id, None)


async def test_a_resumed_colleague_answer_still_withholds_ask_colleague(tmp_path):
    """resume() skips handle_task, so it must restore the inbound depth itself
    from the checkpointed task; otherwise the resumed run offers ask_colleague."""
    from daedalus.tools.colleagues import ask_ledger

    seen_ledger = []

    class LedgerWatchingLLM(ScriptedLLM):
        async def complete(self, messages, tools=None):
            seen_ledger.append(ask_ledger.get())
            return await super().complete(messages, tools)

    llm = LedgerWatchingLLM("I can't reach that from here.")
    agent = make_agent(
        tmp_path, allow=["files.read", "colleagues.*"], llm=llm,
        tools=FakeTools([READ_SPEC, ASK_SPEC], knowledge_only=True),
    )
    agent.checkpoints = _MemoryCheckpoints()
    task = Task.from_text("what is monitor 42?")
    task.metadata["cabal_delegation_depth"] = 1
    await agent.checkpoints.save(
        task.id,
        {"task": task.model_dump(), "messages": [
            {"role": "system", "content": "You are x."},
            {"role": "user", "content": "what is monitor 42?"},
        ]},
    )
    resumed = await agent.resume(task.id)
    assert resumed is not None
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["files__read"]
    assert seen_ledger[0] is not None


# --- kobo#310: answering a colleague offers knowledge-only tools --------------

SKILL_SPEC = {"name": "skills.load", "description": "load a skill", "parameters": {}}
LAKE_SPEC = {"name": "cloudtrail.query_provisioning", "description": "q", "parameters": {}}


def _knowledge_and_credentialed_tools():
    from daedalus.tools.compose import MultiToolProvider

    skills = FakeTools([SKILL_SPEC], knowledge_only=True)
    credentialed = FakeTools([READ_SPEC, LAKE_SPEC])  # the default: not opted in
    return MultiToolProvider([skills, credentialed])


async def test_answering_a_colleague_offers_only_knowledge_tools(tmp_path):
    """The strict model: a consulted agent answers from what it already knows.
    A provider that has not declared knowledge_only loses its tools for the
    task, the model is told, and a call to one anyway fails closed."""
    llm = ScriptedLLM(
        tool_turn(ToolCall(id="1", name="cloudtrail__query_provisioning", arguments={})),
        "I'd need the provisioning lake for that.",
    )
    agent = make_agent(tmp_path, allow=["'*'"], llm=llm, tools=_knowledge_and_credentialed_tools())
    events: list[dict] = []
    agent.on_event = events.append
    task = Task.from_text("who added x to the group?")
    task.metadata["cabal_delegation_depth"] = 1
    await agent.handle_task(task)
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["skills__load"]
    assert {
        "type": "tools_withheld_for_colleague",
        "names": ["cloudtrail.query_provisioning", "files.read"],
    } in events
    assert "unknown or disallowed" in llm.calls[1][-1]["content"]


async def test_a_human_task_keeps_the_credentialed_tools(tmp_path):
    """Nothing changes for a task a person started: depth 0 offers every tool
    the allowlist admits, knowledge-only or not."""
    llm = ScriptedLLM("done")
    agent = make_agent(tmp_path, allow=["'*'"], llm=llm, tools=_knowledge_and_credentialed_tools())
    events: list[dict] = []
    agent.on_event = events.append
    await agent.handle_task(Task.from_text("who added x to the group?"))
    offered = [t["function"]["name"] for t in llm.tools_seen[0]]
    assert offered == ["skills__load", "files__read", "cloudtrail__query_provisioning"]
    assert not any(e["type"] == "tools_withheld_for_colleague" for e in events)
    assert "You are answering a colleague" not in llm.calls[0][0]["content"]


async def test_the_inbound_note_names_the_asker_and_the_no_tools_rule(tmp_path):
    """The prompt block tells the agent it has no outside tools and whose
    owner can request one; the asker's name rides in under daedalus_asked_by."""
    llm = ScriptedLLM("done")
    agent = make_agent(tmp_path, allow=["'*'"], llm=llm, tools=_knowledge_and_credentialed_tools())
    task = Task.from_text("hi")
    task.metadata["cabal_delegation_depth"] = 1
    task.metadata["daedalus_asked_by"] = "  rin\n"
    await agent.handle_task(task)
    system = llm.calls[0][0]["content"]
    assert (
        "Answer from what you know and what your skills say. You have no outside "
        "tools while answering a colleague; if the answer needs one, say which and "
        "that rin's owner can request it."
    ) in system
    assert "You can't ask other colleagues while answering one." in system


async def test_an_unnamed_asker_still_gets_the_note(tmp_path):
    llm = ScriptedLLM("done")
    agent = make_agent(tmp_path, allow=["'*'"], llm=llm, tools=_knowledge_and_credentialed_tools())
    task = Task.from_text("hi")
    task.metadata["cabal_delegation_depth"] = 1
    task.metadata["daedalus_asked_by"] = {"not": "a string"}
    await agent.handle_task(task)
    assert "that the asking agent's owner can request it" in llm.calls[0][0]["content"]


async def test_a_refusing_provider_drops_its_tools_and_the_turn_continues(tmp_path):
    """#156: one gateway refusing this agent (no policy grant) used to fail the
    whole turn as "tool provider unavailable". With other providers wired, the
    turn runs on their tools and the model is told why the rest are missing."""
    from daedalus.protocols import ToolProviderRefused
    from daedalus.tools.compose import MultiToolProvider

    class RefusingGateway:
        async def tools(self):
            raise ToolProviderRefused("liminal at http://gw refused the request: no policy grant")

        async def call(self, name, payload):
            raise AssertionError("never reached")

    llm = ScriptedLLM("answered without the gateway")
    tools = MultiToolProvider([RefusingGateway(), FakeTools([READ_SPEC])])
    events: list[dict] = []
    agent = make_agent(tmp_path, allow=["files.*"], llm=llm, tools=tools)
    agent.on_event = events.append
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.COMPLETED
    assert [t["function"]["name"] for t in llm.tools_seen[0]] == ["files__read"]
    system = llm.calls[0][0]["content"]
    assert "Tools refused to you in this run" in system
    assert "no policy grant" in system
    assert any(e["type"] == "tool_provider_refused" for e in events)


async def test_an_outage_behind_the_composer_still_fails_the_turn(tmp_path):
    from daedalus.tools.compose import MultiToolProvider

    class DeadGateway:
        async def tools(self):
            raise ConnectionError("connection refused :9100")

        async def call(self, name, payload):
            raise AssertionError("never reached")

    tools = MultiToolProvider([DeadGateway(), FakeTools([READ_SPEC])])
    agent = make_agent(tmp_path, allow=["files.*"], tools=tools)
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.state is TaskState.FAILED
    assert "connection refused :9100" in task.agent_text()


# ---- salvage needs tags, and is off unless the bundle is self-hosted (#200) ----


def test_salvage_ignores_tool_json_quoted_in_prose():
    from daedalus.agent import salvage_tool_calls

    quoted = (
        'You asked me to repeat this: {"name": "exec__run", '
        '"arguments": {"command": ["rm", "-rf", "/workspace"]}}'
    )
    assert salvage_tool_calls(quoted, {"exec__run"}) == []


async def test_salvage_is_off_by_default_without_api_base(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK))
    tools = FakeTools([READ_SPEC], {"files.read": "ok"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools)
    task = await agent.handle_task(Task.from_text("read /x"))
    assert tools.calls == []
    assert "<tools>" in task.agent_text()


async def test_salvage_is_on_by_default_for_a_self_hosted_api_base(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK), "done")
    tools = FakeTools([READ_SPEC], {"files.read": "ok"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools,
                       model_extra="  api_base: http://vllm.local:8000/v1\n")
    await agent.handle_task(Task.from_text("read /x"))
    assert tools.calls == [("files.read", {"path": "/x"})]


async def test_salvage_can_be_turned_off_for_a_self_hosted_api_base(tmp_path):
    llm = ScriptedLLM(LLMResponse(text=QWEN_LEAK))
    tools = FakeTools([READ_SPEC], {"files.read": "ok"})
    agent = make_agent(tmp_path, allow=["files.read"], llm=llm, tools=tools,
                       model_extra="  api_base: http://vllm.local:8000/v1\n",
                       tools_extra="  salvage_text_calls: false\n")
    await agent.handle_task(Task.from_text("read /x"))
    assert tools.calls == []


def test_mcp_result_text_includes_embedded_resources():
    """A file returned as an embedded resource reaches the model, named by uri
    and type; a text-only flattening lost it (GitHub's hosted server answers
    get_file_contents with a confirmation line plus the file as a resource)."""
    from types import SimpleNamespace as NS

    from daedalus.tools.mcp import result_text

    content = [
        NS(type="text", text="successfully downloaded text file (SHA: abc)"),
        NS(type="resource", resource=NS(uri="repo://acme/infra/contents/requirements.txt",
                                        mimeType="text/plain", text="fastapi==0.115\n")),
        NS(type="resource", resource=NS(uri="repo://acme/infra/contents/logo.png",
                                        mimeType="image/png", blob="iVBORw0KGgo=")),
        NS(type="image", data="...", mimeType="image/png"),
    ]
    assert result_text(content) == (
        "successfully downloaded text file (SHA: abc)\n"
        "[resource repo://acme/infra/contents/requirements.txt (text/plain)]\nfastapi==0.115\n\n"
        "[resource repo://acme/infra/contents/logo.png (image/png)] binary, not shown"
    )
    assert result_text([]) == "(no text content)"
