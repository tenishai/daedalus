"""The run record a post_draft hook can ask for (#268).

Real Enforcement and a real Bundle with a tools/ dir, as in
test_agent_enforcement.py; ScriptedLLM and FakeTools for the model and tools.
"""

from pathlib import Path

from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.enforcement import _wants_run_record
from daedalus.protocols import LLMResponse, ToolCall, ToolCallBlocked, ToolResult
from daedalus.run_record import RunRecorder, tool_outcome
from daedalus.task import ReasonCode, Task, TaskState
from tests.fakes import FakeTools, ScriptedLLM

READ_SPEC = {"name": "files.read", "description": "read a file", "parameters": {}}

# A hook that renders the record into the reply, so a test can read what it got.
FOOTER_HOOK = (
    "def post_draft(draft, record):\n"
    "    calls = ','.join(f'{c.name}:{c.outcome}' for c in record.tool_calls)\n"
    "    u = record.usage_total\n"
    "    return (f'{draft} | engine={record.engine} model={record.model} '\n"
    "            f'turns={record.turns} calls={calls} '\n"
    "            f'in={u.input_tokens} out={u.output_tokens} '\n"
    "            f'cache_read={u.cache_read_input_tokens} '\n"
    "            f'total_in={record.total_input_tokens} limit={record.limit}')\n"
)


def make_agent(tmp_path: Path, *, hooks: dict[str, str], llm, tools, tools_extra=""):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: none\n" + tools_extra
        + "  allow:\n    - files.read\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    hooks_dir = tmp_path / "tools"
    hooks_dir.mkdir()
    for name, body in hooks.items():
        (hooks_dir / name).write_text(body)
    return Agent(Bundle.load(tmp_path), llm=llm, tools=tools)


def read_turn(call_id: str, **usage) -> LLMResponse:
    return LLMResponse(
        text="", tool_calls=[ToolCall(id=call_id, name="files__read", arguments={})], **usage
    )


async def test_a_record_taking_hook_sees_the_tools_turns_and_tokens(tmp_path):
    llm = ScriptedLLM(
        read_turn("1", input_tokens=100, output_tokens=10, cache_read_input_tokens=60),
        read_turn("2", input_tokens=200, output_tokens=20, cache_read_input_tokens=150),
        LLMResponse(text="answer", input_tokens=300, output_tokens=30, cache_read_input_tokens=250),
    )
    tools = FakeTools([READ_SPEC], {"files.read": "contents"})
    agent = make_agent(tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm, tools=tools)

    task = await agent.handle_task(Task.from_text("read twice"))

    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == (
        "answer | engine=litellm model=m turns=3 "
        "calls=files.read:ok,files.read:ok in=600 out=60 cache_read=460 "
        "total_in=600 limit=None"
    )


async def test_an_unreported_turn_makes_the_total_unreported(tmp_path):
    llm = ScriptedLLM(
        read_turn("1", input_tokens=100, output_tokens=10),
        LLMResponse(text="answer"),  # the provider reported nothing
    )
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": "x"}),
    )
    task = await agent.handle_task(Task.from_text("q"))
    assert "in=None out=None" in task.agent_text()
    assert "total_in=None" in task.agent_text()


async def test_the_record_names_the_limit_a_run_hit(tmp_path):
    llm = ScriptedLLM(*[read_turn(str(i), input_tokens=1, output_tokens=1) for i in range(5)])
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": "x"}), tools_extra="  max_steps: 2\n",
    )
    task = await agent.handle_task(Task.from_text("loop"))
    assert task.reason_code is ReasonCode.POLICY_VIOLATION
    assert "turns=2" in task.agent_text() and "limit=steps" in task.agent_text()


async def test_empty_and_refused_tool_results_have_their_own_outcomes(tmp_path):
    llm = ScriptedLLM(
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(id="1", name="files__read", arguments={}),
                ToolCall(id="2", name="nope__missing", arguments={}),
            ],
        ),
        "done",
    )
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": ""}),
    )
    task = await agent.handle_task(Task.from_text("q"))
    assert "calls=files.read:empty,nope__missing:error" in task.agent_text()


class OutcomeTools(FakeTools):
    """FakeTools whose files.read answers with a fixed value, or raises it."""

    def __init__(self, answer):
        super().__init__([READ_SPEC])
        self.answer = answer

    async def call(self, name, payload):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def _outcome_through_agent(tmp_path, answer) -> str:
    llm = ScriptedLLM(read_turn("1"), "done")
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm, tools=OutcomeTools(answer)
    )
    task = await agent.handle_task(Task.from_text("q"))
    return task.agent_text().split("calls=")[1].split(" ")[0]


async def test_a_real_result_that_starts_with_error_reads_ok(tmp_path):
    """The outcome comes from the code path (#268): the provider returned it, so
    it is a result, whatever its first word."""
    answer = "error: 3 matches in tests/test_errors.py"
    assert await _outcome_through_agent(tmp_path, answer) == "files.read:ok"


async def test_a_providers_own_outcome_is_kept(tmp_path):
    """A provider that saw an empty result (the gateway's envelope makes the
    text non-empty) says so, and the record keeps it."""
    answer = ToolResult("[BEGIN ...][END ...]", "empty")
    assert await _outcome_through_agent(tmp_path, answer) == "files.read:empty"


async def test_a_policy_refusal_from_a_provider_reads_blocked(tmp_path):
    answer = ToolCallBlocked("liminal refused files.read: scope 'files:read' required")
    assert await _outcome_through_agent(tmp_path, answer) == "files.read:blocked"


async def test_a_provider_exception_reads_error(tmp_path):
    assert await _outcome_through_agent(tmp_path, RuntimeError("boom")) == "files.read:error"


async def test_a_repeated_call_keeps_its_outcome_under_the_repeat_note(tmp_path):
    """The LiteLLM engine appends a note to a repeated call's result, and a str
    `+` drops ToolResult.outcome: it is read before the note."""
    llm = ScriptedLLM(read_turn("1"), read_turn("2"), "done")
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm,
        tools=OutcomeTools("error: 3 matches in tests/test_errors.py"),
        tools_extra="  max_repeats: 3\n",
    )
    task = await agent.handle_task(Task.from_text("q"))
    assert "calls=files.read:ok,files.read:ok " in task.agent_text()


async def test_a_hook_written_with_record_none_gets_the_record(tmp_path):
    """The recommended form (docs/arch/approvals.md): an older runtime calls it
    with one argument, this one passes the record."""
    hook = (
        "def post_draft(draft, record=None):\n"
        "    return draft if record is None else f'{draft} turns={record.turns}'\n"
    )
    agent = make_agent(
        tmp_path, hooks={"a_hook.py": hook}, llm=ScriptedLLM("hi"), tools=FakeTools([READ_SPEC])
    )
    task = await agent.handle_task(Task.from_text("q"))
    assert task.agent_text() == "hi turns=1"


async def test_a_finished_task_logs_its_usage_once(tmp_path, caplog):
    llm = ScriptedLLM(
        read_turn("1", input_tokens=100, output_tokens=10, cache_read_input_tokens=60,
                  cache_creation_input_tokens=5),
        LLMResponse(text="answer", input_tokens=200, output_tokens=20,
                    cache_read_input_tokens=150, cache_creation_input_tokens=0),
    )
    agent = make_agent(
        tmp_path, hooks={}, llm=llm, tools=FakeTools([READ_SPEC], {"files.read": "x"})
    )
    with caplog.at_level("INFO", logger="daedalus.agent"):
        await agent.handle_task(Task.from_text("q"))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("task usage")]
    assert lines == [
        "task usage: engine=litellm model=m turns=2 input=300 output=30 "
        "cache_read=210 cache_write=5"
    ]


async def test_unreported_usage_logs_as_unknown(tmp_path, caplog):
    agent = make_agent(
        tmp_path, hooks={}, llm=ScriptedLLM(LLMResponse(text="answer")),
        tools=FakeTools([READ_SPEC]),
    )
    with caplog.at_level("INFO", logger="daedalus.agent"):
        await agent.handle_task(Task.from_text("q"))
    assert (
        "task usage: engine=litellm model=m turns=1 input=unknown output=unknown "
        "cache_read=unknown cache_write=unknown"
    ) in [r.getMessage() for r in caplog.records]


async def test_a_one_parameter_hook_still_works_beside_a_record_taking_one(tmp_path):
    hooks = {
        "a_shout.py": "def post_draft(draft):\n    return draft.upper()\n",
        "b_footer.py": FOOTER_HOOK,
    }
    agent = make_agent(
        tmp_path, hooks=hooks, llm=ScriptedLLM("hello"), tools=FakeTools([READ_SPEC])
    )
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.agent_text().startswith("HELLO | engine=litellm")


async def test_a_record_taking_hook_that_raises_still_discards_the_draft(tmp_path):
    hooks = {
        "z_reject.py": (
            "def post_draft(draft, record):\n"
            "    raise ValueError(f'{record.turns} turn(s) is too few')\n"
        )
    }
    agent = make_agent(
        tmp_path, hooks=hooks, llm=ScriptedLLM("the secret is 42"), tools=FakeTools([READ_SPEC])
    )
    task = await agent.handle_task(Task.from_text("hi"))
    assert task.reason_code is ReasonCode.TOOL_BLOCKED
    assert task.agent_text() == (
        "A rule stopped this reply: blocked by z_reject.py: 1 turn(s) is too few."
    )
    assert all("the secret is 42" not in m.text for m in task.history)


async def test_a_task_does_not_see_the_previous_tasks_record(tmp_path):
    llm = ScriptedLLM(read_turn("1", input_tokens=5, output_tokens=5), "first", "second")
    agent = make_agent(
        tmp_path, hooks={"a_footer.py": FOOTER_HOOK}, llm=llm,
        tools=FakeTools([READ_SPEC], {"files.read": "x"}),
    )
    await agent.handle_task(Task.from_text("one"))
    second = await agent.handle_task(Task.from_text("two"))
    assert "turns=1 calls= " in second.agent_text()


# ---- the pieces, directly ---------------------------------------------------


def test_signature_decides_whether_a_hook_gets_the_record():
    def one(draft):
        return draft

    def two(draft, record):
        return draft

    def star(*args):
        return args[0]

    def defaulted(draft, record=None):
        return draft

    def keyword_only(draft, *, record=None):
        return draft

    assert not _wants_run_record(one)
    assert _wants_run_record(two)
    assert _wants_run_record(star)
    assert _wants_run_record(defaulted)
    assert not _wants_run_record(keyword_only)
    assert not _wants_run_record(len)  # a builtin with no readable signature


def test_claude_code_totals_come_from_engine_finished():
    """Claude Code reports one total per run; its input_tokens excludes the cache,
    so total_input_tokens adds the cache counts back."""
    recorder = RunRecorder("claude-code", "claude-opus-5-5")
    recorder.observe({"type": "tool_call", "name": "claude.Bash", "arguments": {}})
    recorder.observe({"type": "tool_call", "name": "files.read", "arguments": {}})
    recorder.observe({"type": "tool_result", "name": "files.read", "content": "x"})
    recorder.observe({
        "type": "engine_finished", "engine": "claude-code", "turns": 4,
        "input_tokens": 50, "output_tokens": 7,
        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
        "total_cost_usd": 0.02,
    })
    record = recorder.record()
    assert record.turns == 4 and record.usage_per_turn == ()
    assert record.total_input_tokens == 1250
    assert record.cost_usd == 0.02
    assert [(c.name, c.outcome) for c in record.tool_calls] == [
        ("claude.Bash", "unknown"), ("files.read", "ok")
    ]
    assert record.tool_calls[0].duration_s is None
    assert record.tool_calls[1].duration_s is not None


def test_tool_outcome_reads_the_agents_own_prefixes():
    assert tool_outcome("contents") == "ok"
    assert tool_outcome("  ") == "empty"
    assert tool_outcome("(no text content)") == "empty"
    assert tool_outcome("blocked: files.write requires approval (plan_approval)") == "blocked"
    assert tool_outcome("error: unknown or disallowed tool 'x'") == "error"
    assert tool_outcome("tool error: boom") == "error"


def test_an_event_outcome_wins_over_the_text():
    recorder = RunRecorder("litellm", "m")
    recorder.observe({"type": "tool_call", "name": "github.search", "arguments": {}})
    recorder.observe({
        "type": "tool_result", "name": "github.search",
        "content": "error: 3 matches", "outcome": "ok",
    })
    assert recorder.record().tool_calls[0].outcome == "ok"


def test_a_denied_builtin_reads_blocked_in_either_order():
    """Claude Code traces a built-in's tool_call from the assistant message, and
    a gate's refusal (Agent._approve_native) as permission_denied; the order of
    the two is the SDK's."""
    after = RunRecorder("claude-code", "m")
    after.observe({"type": "tool_call", "name": "claude.Bash", "arguments": {}})
    after.observe({"type": "permission_denied", "name": "claude.Bash"})
    before = RunRecorder("claude-code", "m")
    before.observe({"type": "permission_denied", "name": "claude.Bash"})
    before.observe({"type": "tool_call", "name": "claude.Bash", "arguments": {}})
    before.observe({"type": "tool_call", "name": "claude.Bash", "arguments": {}})
    assert [c.outcome for c in after.record().tool_calls] == ["blocked"]
    assert [c.outcome for c in before.record().tool_calls] == ["blocked", "unknown"]


async def test_the_verbose_printer_shows_one_usage_line_per_task(tmp_path):
    """Pods run `daedalus run -v` with no log handler, so the INFO line is
    dropped there; the -v printer is what reaches the pod log. One line per
    task, from `task_usage`; the per-call `usage` events print nothing."""
    from click.testing import CliRunner

    from daedalus.cli import _print_event

    llm = ScriptedLLM(
        read_turn("1", input_tokens=100, output_tokens=10, cache_read_input_tokens=60),
        LLMResponse(text="first", input_tokens=200, output_tokens=20),
        LLMResponse(text="second"),
    )
    agent = make_agent(
        tmp_path, hooks={}, llm=llm, tools=FakeTools([READ_SPEC], {"files.read": "x"})
    )
    agent.on_event = _print_event
    runner = CliRunner()
    with runner.isolation() as (out, _err, _):
        await agent.handle_task(Task.from_text("one"))
        await agent.handle_task(Task.from_text("two"))
        printed = out.getvalue().decode()
    lines = [line for line in printed.splitlines() if "[usage]" in line]
    assert lines == [
        "  [usage] task: engine=litellm model=m turns=2 input=300 output=30 "
        "cache_read=unknown cache_write=unknown",
        "  [usage] task: engine=litellm model=m turns=1 input=unknown output=unknown "
        "cache_read=unknown cache_write=unknown",
    ]
