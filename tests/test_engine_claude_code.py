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


_unset_usage = object()  # lets a test pass usage=None, distinct from "use the default"


def _result(subtype="success", *, is_error=False, result="done", model_usage=None,
            usage=_unset_usage, total_cost_usd=None):
    return sdk.ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=2,
        session_id="s", result=result,
        usage={"input_tokens": 3, "output_tokens": 4} if usage is _unset_usage else usage,
        model_usage=model_usage or {}, total_cost_usd=total_cost_usd,
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
    assert o.cli_path is None  # the SDK then uses its bundled CLI (tenishai/agents#51)
    assert o.model is None


def test_options_pass_a_configured_binary(tmp_path):
    o = ClaudeCodeEngine(ClaudeCodeSpec(binary="/opt/claude")).options(_turn(tmp_path), sdk, "p")
    assert o.cli_path == "/opt/claude"


def test_options_pass_the_pinned_model_and_config_dir(tmp_path):
    spec = ClaudeCodeSpec(model="claude-opus-5-5", config_dir="/cfg/prudence")
    o = ClaudeCodeEngine(spec).options(_turn(tmp_path), sdk, "p")
    assert o.model == "claude-opus-5-5"
    assert o.env["CLAUDE_CONFIG_DIR"] == "/cfg/prudence"


def test_options_pass_the_working_directory(tmp_path):
    """#242: the bundle's cwd is where Claude Code works; unset, none is passed."""
    o = ClaudeCodeEngine(ClaudeCodeSpec(cwd=str(tmp_path))).options(_turn(tmp_path), sdk, "p")
    assert str(o.cwd) == str(tmp_path)
    assert ClaudeCodeEngine(ClaudeCodeSpec()).options(_turn(tmp_path), sdk, "p").cwd is None


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
        return None if name == "claude.Bash" else f"blocked: {name}"

    events = []
    engine = ClaudeCodeEngine(ClaudeCodeSpec())
    decide = engine.options(_turn(tmp_path, approve=approve, events=events), sdk, "p").can_use_tool
    ours = await decide("mcp__daedalus__files__read", {"path": "/x"}, None)
    bash = await decide("Bash", {"command": "ls"}, None)
    spoof = await decide("mcp__daedalus__evil__Bash", {"command": "rm -rf ~"}, None)
    assert isinstance(ours, sdk.PermissionResultAllow) and ours.updated_input == {"path": "/x"}
    assert isinstance(bash, sdk.PermissionResultAllow)
    assert isinstance(spoof, sdk.PermissionResultDeny)
    assert spoof.message == "blocked: claude.mcp__daedalus__evil__Bash"
    assert spoof.interrupt is False
    assert asked == ["claude.Bash", "claude.mcp__daedalus__evil__Bash"]  # ours never asked
    assert ("permission_denied", {"name": "claude.mcp__daedalus__evil__Bash"}) in events


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
    assert ("engine_finished", {
        "engine": "claude-code", "models": ["claude-opus-5-5"], "turns": 2,
        "model_usage": {"claude-opus-5-5": {}},
        "input_tokens": 3, "output_tokens": 4,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }) in events  # no total_cost_usd: _result()'s default is None, so it's left out


async def test_engine_finished_reports_the_full_numbers_when_the_sdk_gives_them(tmp_path):
    """daedalus#135: the real counters, not just which models ran."""
    events = []
    usage = {
        "input_tokens": 1_200_000, "output_tokens": 11_234,
        "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 200_000,
    }
    result = _result(usage=usage, model_usage={"claude-opus-5-5": {"inputTokens": 1_200_000}},
                     total_cost_usd=1.234)
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=_fake_query(result)).run(
        _turn(tmp_path, events=events))
    assert task.state is not TaskState.FAILED
    finished = next(f for k, f in events if k == "engine_finished")
    assert finished == {
        "engine": "claude-code", "models": ["claude-opus-5-5"], "turns": 2,
        "model_usage": {"claude-opus-5-5": {"inputTokens": 1_200_000}},
        "input_tokens": 1_200_000, "output_tokens": 11_234,
        "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 200_000,
        "total_cost_usd": 1.234,
    }


async def test_engine_finished_omits_token_fields_when_usage_is_missing(tmp_path):
    """An older SDK, or an error result, can leave usage/model_usage/total_cost_usd
    None; the engine must not raise, and must not report measured zeros for numbers
    nobody measured."""
    events = []
    result = _result(usage=None, model_usage=None, total_cost_usd=None)
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=_fake_query(result)).run(
        _turn(tmp_path, events=events))
    assert task.state is not TaskState.FAILED
    finished = next(f for k, f in events if k == "engine_finished")
    assert finished == {
        "engine": "claude-code", "models": [], "turns": 2, "model_usage": {},
    }
    assert "input_tokens" not in finished and "total_cost_usd" not in finished


async def test_an_error_result_still_reports_usage(tmp_path):
    """The event is emitted before the error/max-turns branches (daedalus#135): a
    failed run still says what it spent."""
    events = []
    usage = {"input_tokens": 5, "output_tokens": 1}
    result = _result("error_during_execution", is_error=True, result=None, usage=usage,
                     model_usage={"claude-opus-5-5": {}}, total_cost_usd=0.01)
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=_fake_query(result)).run(
        _turn(tmp_path, events=events))
    assert task.state is TaskState.FAILED
    finished = next(f for k, f in events if k == "engine_finished")
    assert finished["input_tokens"] == 5 and finished["total_cost_usd"] == 0.01


async def test_running_out_of_turns_is_a_policy_stop(tmp_path):
    query = _fake_query(_result("error_max_turns", is_error=True, result=None))
    task = await ClaudeCodeEngine(ClaudeCodeSpec(), query=query).run(_turn(tmp_path))
    assert task.state is TaskState.FAILED
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert "10 turns" in task.agent_text()


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
    assert "tenish-daedalus[claude-code]" in task.agent_text()


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
    turn = _turn(tmp_path, events=events)
    turn.withheld = lambda name: name == "claude.Bash"  # the Agent's rule, stubbed
    o = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Read", "Bash"])).options(turn, sdk, "p")
    assert o.tools == ["Read"]
    assert "Built-in tools you can't use in this run" in o.system_prompt
    assert "Bash" in o.system_prompt.split("run")[-1]
    assert ("tools_withheld", {"names": ["claude.Bash"]}) in events


def test_with_an_approver_gated_builtins_stay_offered(tmp_path):
    turn = _turn(tmp_path)
    turn.withheld = lambda name: False
    o = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Read", "Bash"])).options(turn, sdk, "p")
    assert o.tools == ["Read", "Bash"] and o.system_prompt == "p"


def test_the_agent_withholds_a_gated_native_tool_only_when_nobody_can_approve(tmp_path):
    from tests.fakes import RecordingApprover

    gates = "    - match: 'claude.Bash'\n      require: human\n"
    headless = make_agent(tmp_path, allow=["files.read"], gates=gates)
    assert headless._withheld_native("claude.Bash") is True
    assert headless._withheld_native("claude.Read") is False  # no gate
    (tmp_path / "b").mkdir()
    wired = make_agent(tmp_path / "b", allow=["files.read"], gates=gates,
                       approver=RecordingApprover(True))
    assert wired._withheld_native("claude.Bash") is False


async def test_a_gate_written_from_the_trace_fires(tmp_path):
    """Prudence, PR 108 r1: the trace says claude.Bash, so a gate on claude.* must catch
    Bash. Before, gates saw the bare name and claude.* matched nothing (fail open)."""
    from tests.fakes import RecordingApprover

    approver = RecordingApprover(False)
    gates = "    - match: 'claude.*'\n      require: human\n"
    agent = make_agent(tmp_path, allow=["files.read"], gates=gates, approver=approver)
    engine = ClaudeCodeEngine(ClaudeCodeSpec(builtin_tools=["Bash"]))
    turn = _turn(tmp_path, approve=agent._approve_native)
    turn.spec = agent.bundle.spec
    decide = engine.options(turn, sdk, "p").can_use_tool
    decision = await decide("Bash", {"command": "ls"}, None)
    assert isinstance(decision, sdk.PermissionResultDeny)
    assert approver.asked and approver.asked[0][0] == "claude.Bash"


def test_a_token_budget_is_refused_with_this_engine(tmp_path):
    """Prudence, PR 108 r1: per_task_tokens can't stop Claude Code between calls, so
    loading refuses instead of silently not enforcing it."""
    make_agent(tmp_path, allow=["files.read"])
    text = (tmp_path / "agent.yaml").read_text().replace(
        "model:\n  id: m\n", "model:\n  id: m\n  budget:\n    per_task_tokens: 50000\n"
    )
    (tmp_path / "agent.yaml").write_text(text)
    assert Agent(Bundle.load(tmp_path)).engine_name == "litellm"  # fine there
    with pytest.raises(BundleError, match="can't enforce it"):
        Agent(Bundle.load(tmp_path), engine="claude-code")


def test_a_working_directory_that_is_not_one_is_refused_at_load(tmp_path):
    """#242: a missing cwd fails the load with this engine; the litellm engine, which
    never uses it, still loads the same bundle."""
    make_agent(tmp_path, allow=["files.read"])
    missing = tmp_path / "no-such-worktree"
    with (tmp_path / "agent.yaml").open("a") as f:
        f.write(f"engine:\n  claude_code:\n    cwd: {missing}\n")
    assert Agent(Bundle.load(tmp_path)).engine_name == "litellm"
    with pytest.raises(BundleError, match="is not a directory"):
        Agent(Bundle.load(tmp_path), engine="claude-code")
    missing.mkdir()
    assert Agent(Bundle.load(tmp_path), engine="claude-code").engine_name == "claude-code"



@pytest.mark.parametrize("binary, words", [
    (None, "bundled with claude-agent-sdk"),
    ("/opt/claude", "can't run `/opt/claude`"),
])
async def test_a_missing_cli_names_where_it_looked(tmp_path, binary, words):
    async def query(*, prompt, options):
        raise sdk.CLINotFoundError("not found")
        yield  # pragma: no cover - makes this an async generator

    task = await ClaudeCodeEngine(ClaudeCodeSpec(binary=binary), query=query).run(_turn(tmp_path))
    assert task.state is TaskState.FAILED
    assert words in task.agent_text()
    assert "--engine litellm" in task.agent_text()


async def test_a_tool_handler_passes_the_call_tool_outcome_to_the_trace(tmp_path):
    """#268: the run record reads the outcome the code path set, not the text."""
    from daedalus.protocols import ToolResult

    events = []
    turn = _turn(tmp_path, events=events)

    async def call_tool(name, arguments):
        return ToolResult("error: 2 hits in errors.py", "ok")

    turn.call_tool = call_tool
    tools, _ = ClaudeCodeEngine(ClaudeCodeSpec())._our_tools(turn, sdk)
    out = await tools[0].handler({})
    assert type(out["content"][0]["text"]) is str
    assert events[-1] == (
        "tool_result",
        {"name": "files.read", "content": "error: 2 hits in errors.py", "outcome": "ok"},
    )
