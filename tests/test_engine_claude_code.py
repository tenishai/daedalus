"""The Claude Code engine (tenishai/agents#51): options it hands the Agent SDK, the
approval callback, the tool handlers, and how a result becomes a task. No live Claude:
a fake `query` yields real SDK message objects."""

import sys

import pytest

sdk = pytest.importorskip("claude_agent_sdk", reason="the claude-code extra is not installed")

from daedalus.agent import Agent  # noqa: E402
from daedalus.bundle import Bundle, BundleError, ClaudeCodeSpec  # noqa: E402
from daedalus.engines import EngineTurn  # noqa: E402
from daedalus.engines.claude_code import ClaudeCodeEngine  # noqa: E402
from daedalus.protocols import NoBudget, NoCheckpoints  # noqa: E402
from daedalus.task import ReasonCode, Task, TaskState  # noqa: E402
from tests.test_agent_tools import READ_SPEC, make_agent  # noqa: E402


def _turn(tmp_path, *, tools=(READ_SPEC,), approve: object = "unset", events=None, calls=None):
    agent = make_agent(tmp_path, allow=["files.read"])

    async def call_tool(name, arguments):
        if calls is not None:
            calls.append((name, arguments))
        return f"ran {name}"

    async def default_approve(name, arguments):
        return None

    return EngineTurn(
        task=Task.from_text("go"),
        messages=[{"role": "system", "content": "persona"}, {"role": "user", "content": "go"}],
        tools=list(tools),
        call_tool=call_tool,
        emit=(lambda kind, **f: events.append((kind, f))) if events is not None
        else (lambda *a, **k: None),
        budget=NoBudget(),
        checkpoints=NoCheckpoints(),
        spec=agent.bundle.spec,
        approve=default_approve if approve == "unset" else approve,
    )


def _result(subtype="success", *, is_error=False, result="done", model_usage=None):
    return sdk.ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=2,
        session_id="s", result=result, usage={"input_tokens": 3, "output_tokens": 4},
        model_usage=model_usage or {},
    )


def _fake_query(*messages, seen=None):
    async def query(*, prompt, options):
        if seen is not None:
            seen.append((prompt, options))
        for m in messages:
            yield m
    return query


# -- options ---------------------------------------------------------------------


def test_options_keep_workspace_settings_out_and_only_our_tools_pre_approved(tmp_path):
    engine = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Read", "Bash"]))
    o = engine.options(_turn(tmp_path), sdk, "persona")
    assert o.setting_sources == ["user"]
    assert o.strict_mcp_config is True
    assert o.tools == ["Read", "Bash"]
    assert o.allowed_tools == ["mcp__daedalus__files__read"]  # built-ins never pre-approved
    assert o.permission_mode == "default"
    assert o.system_prompt == "persona"
    assert o.max_turns == 10
    assert o.env["DAEDALUS_ENGINE_CHILD"] == "1"
    assert "CLAUDE_CONFIG_DIR" not in o.env
    assert o.cli_path == "claude"
    assert o.model is None


def test_options_pass_the_pinned_model_and_config_dir(tmp_path):
    spec = ClaudeCodeSpec(model="claude-opus-5-5", config_dir="/cfg/prudence")
    o = ClaudeCodeEngine(spec).options(_turn(tmp_path), sdk, "p")
    assert o.model == "claude-opus-5-5"
    assert o.env["CLAUDE_CONFIG_DIR"] == "/cfg/prudence"


def test_with_nobody_to_approve_claude_codes_unattended_mode_is_used(tmp_path):
    o = ClaudeCodeEngine(ClaudeCodeSpec()).options(_turn(tmp_path, approve=None), sdk, "p")
    assert o.can_use_tool is None
    assert o.extra_args == {"permission-prompts": "none"}


def test_with_an_approver_the_callback_is_used(tmp_path):
    o = ClaudeCodeEngine(ClaudeCodeSpec()).options(_turn(tmp_path), sdk, "p")
    assert o.can_use_tool is not None
    assert "permission-prompts" not in (o.extra_args or {})


# -- approvals -------------------------------------------------------------------


async def test_can_use_tool_allows_our_tools_and_gates_everything_else(tmp_path):
    asked = []

    async def approve(name, arguments):
        asked.append(name)
        return None if name == "Bash" else f"blocked: {name}"

    events = []
    engine = ClaudeCodeEngine(ClaudeCodeSpec())
    decide = engine.options(_turn(tmp_path, approve=approve, events=events), sdk, "p").can_use_tool
    ours = await decide("mcp__daedalus__files__read", {"path": "/x"}, None)
    bash = await decide("Bash", {"command": "ls"}, None)
    spoof = await decide("mcp__daedalus__evil__Bash", {"command": "rm -rf ~"}, None)
    assert isinstance(ours, sdk.PermissionResultAllow) and ours.updated_input == {"path": "/x"}
    assert isinstance(bash, sdk.PermissionResultAllow)
    assert isinstance(spoof, sdk.PermissionResultDeny)
    assert spoof.message == "blocked: mcp__daedalus__evil__Bash" and spoof.interrupt is False
    assert asked == ["Bash", "mcp__daedalus__evil__Bash"]  # ours never asked
    assert ("permission_denied", {"name": "mcp__daedalus__evil__Bash"}) in events


async def test_on_denied_stop_interrupts_the_run(tmp_path):
    async def refuse(name, arguments):
        return "blocked"

    engine = ClaudeCodeEngine(ClaudeCodeSpec(on_denied="stop"))
    decide = engine.options(_turn(tmp_path, approve=refuse), sdk, "p").can_use_tool
    assert (await decide("Bash", {}, None)).interrupt is True


# -- tools -----------------------------------------------------------------------


async def test_a_tool_handler_goes_through_call_tool_by_canonical_name(tmp_path):
    calls, events = [], []
    engine = ClaudeCodeEngine(ClaudeCodeSpec())
    tools, canonical = engine._our_tools(_turn(tmp_path, calls=calls, events=events), sdk)
    assert canonical == {"files__read": "files.read"}
    out = await tools[0].handler({"path": "/x"})
    assert out == {"content": [{"type": "text", "text": "ran files.read"}]}
    assert calls == [("files.read", {"path": "/x"})]
    assert [k for k, _ in events] == ["tool_call", "tool_result"]


async def test_two_tools_with_one_model_facing_name_fail_loudly(tmp_path):
    clash = {"name": "files__read", "description": "", "parameters": {}}
    turn = _turn(tmp_path, tools=(READ_SPEC, clash))
    engine = ClaudeCodeEngine(ClaudeCodeSpec(), query=_fake_query(_result()))
    task = await engine.run(turn)
    assert task.state is TaskState.FAILED
    assert "both become 'files__read'" in task.agent_text()


# -- results ---------------------------------------------------------------------


async def test_a_successful_run_replies_with_the_result_and_records_models(tmp_path):
    events, seen = [], []
    query = _fake_query(
        sdk.SystemMessage(subtype="init", data={}),
        sdk.AssistantMessage(content=[
            sdk.ThinkingBlock(thinking="hmm", signature="x"),
            sdk.ToolUseBlock(id="1", name="Bash", input={"command": "ls"}),
            sdk.ToolUseBlock(id="2", name="mcp__daedalus__files__read", input={}),
        ], model="m"),
        _result(model_usage={"claude-opus-5-5": {}}),
        seen=seen,
    )
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=query).run(
        _turn(tmp_path, events=events))
    assert task.state is not TaskState.FAILED and task.agent_text() == "done"
    assert seen[0][0] == "go"
    kinds = [(k, f.get("name")) for k, f in events]
    assert ("reasoning", None) in kinds and ("tool_call", "claude.Bash") in kinds
    assert ("tool_call", "mcp__daedalus__files__read") not in kinds  # traced in the handler
    assert ("engine_finished", {"engine": "claude-code", "models": ["claude-opus-5-5"],
                                "turns": 2}) in events


async def test_running_out_of_turns_is_a_policy_stop(tmp_path):
    query = _fake_query(_result("error_max_turns", is_error=True, result=None))
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=query).run(_turn(tmp_path))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert "10 tool steps" in task.agent_text()


async def test_an_error_result_or_no_result_fails_as_escalation(tmp_path):
    error = await ClaudeCodeEngine(
        ClaudeCodeSpec(), query=_fake_query(_result("error_during_execution", is_error=True,
                                                     result=None))).run(_turn(tmp_path))
    none = await ClaudeCodeEngine(ClaudeCodeSpec(), query=_fake_query()).run(_turn(tmp_path))
    assert error.reason_code is ReasonCode.ESCALATION
    assert "error_during_execution" in error.agent_text()
    assert none.reason_code is ReasonCode.ESCALATION
    assert "without a result" in none.agent_text()


async def test_a_missing_sdk_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    task = await ClaudeCodeEngine(ClaudeCodeSpec()).run(_turn(tmp_path))
    assert task.state is TaskState.FAILED
    assert "daedalus[claude-code]" in task.agent_text()


# -- choosing the engine ---------------------------------------------------------


def test_the_engine_is_the_operators_choice(tmp_path, monkeypatch):
    make_agent(tmp_path, allow=["files.read"])  # writes the bundle
    assert Agent(Bundle.load(tmp_path)).engine_name == "litellm"
    assert isinstance(Agent(Bundle.load(tmp_path), engine="claude-code")._engine(),
                      ClaudeCodeEngine)
    monkeypatch.setenv("DAEDALUS_ENGINE", "claude-code")
    assert Agent(Bundle.load(tmp_path)).engine_name == "claude-code"
    with pytest.raises(BundleError, match="unknown engine 'codex'"):
        Agent(Bundle.load(tmp_path), engine="codex")


def test_bundle_rejects_an_unknown_permission_mode(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\nengine:\n  claude_code:\n"
        "    permission_mode: bypassPermissions\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("p")
    with pytest.raises((BundleError, ValueError)):
        Bundle.load(tmp_path)


def test_with_nobody_to_approve_gated_builtins_are_withheld_and_explained(tmp_path):
    """Temperance, PR 108 r1: with the DenyAll approver the callback refused a gated
    built-in on every call. Now it isn't offered, and the model is told; ungated
    built-ins stay usable (Prudence runs tests with Bash, unattended)."""
    events = []
    gates = "    - match: 'Bash'\n      require: human\n"
    agent = make_agent(tmp_path, allow=["files.read"], gates=gates)
    turn = _turn(tmp_path, events=events)
    turn.spec = agent.bundle.spec
    turn.can_approve = False
    o = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Read", "Bash"])).options(turn, sdk, "p")
    assert o.tools == ["Read"]
    assert "Built-in tools you can't use in this run" in o.system_prompt
    assert "Bash" in o.system_prompt.split("run")[-1]
    assert ("tools_withheld", {"names": ["claude.Bash"]}) in events


def test_with_an_approver_gated_builtins_stay_offered(tmp_path):
    gates = "    - match: 'Bash'\n      require: human\n"
    agent = make_agent(tmp_path, allow=["files.read"], gates=gates)
    turn = _turn(tmp_path)
    turn.spec = agent.bundle.spec
    o = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Read", "Bash"])).options(turn, sdk, "p")
    assert o.tools == ["Read", "Bash"] and o.system_prompt == "p"


async def test_the_agent_tells_the_engine_whether_anyone_can_approve(tmp_path):
    from tests.fakes import RecordingApprover

    seen = []

    class Capture:
        name = "capture"

        async def run(self, turn):
            seen.append(turn.can_approve)
            return turn.task.reply("ok")

    headless = make_agent(tmp_path, allow=["files.read"])
    headless._engine = lambda: Capture()
    await headless.handle_task(Task.from_text("x"))
    (tmp_path / "b").mkdir()
    wired = make_agent(tmp_path / "b", allow=["files.read"], approver=RecordingApprover(True))
    wired._engine = lambda: Capture()
    await wired.handle_task(Task.from_text("x"))
    assert seen == [False, True]

