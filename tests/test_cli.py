from click.testing import CliRunner

import daedalus
from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.cli import main
from daedalus.task import RECALL_KEY
from tests.fakes import ScriptedLLM


def test_version():
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert daedalus.__version__ in result.output


def test_create_scaffolds_a_loadable_bundle(tmp_path):
    result = CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    bundle = Bundle.load(tmp_path / "scout")
    assert bundle.spec.name == "scout"
    assert bundle.card["name"] == "scout"
    assert [t.kind for t in bundle.spec.transports] == ["cli"]


def test_create_refuses_to_overwrite(tmp_path):
    runner = CliRunner()
    assert runner.invoke(main, ["create", "scout", "--dir", str(tmp_path)]).exit_code == 0
    result = runner.invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_run_one_shot_prints_reply(tmp_path, monkeypatch):
    CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    real_load = Agent.load.__func__

    def load_with_fake_llm(cls, path, **overrides):
        overrides.setdefault("llm", ScriptedLLM("scout reporting in"))
        return real_load(cls, path, **overrides)

    monkeypatch.setattr(Agent, "load", classmethod(load_with_fake_llm))
    result = CliRunner().invoke(main, ["run", str(tmp_path / "scout"), "-m", "status?"])
    assert result.exit_code == 0, result.output
    assert "scout reporting in" in result.output


def test_run_verbose_prints_tool_trace(tmp_path, monkeypatch):
    from daedalus.protocols import LLMResponse, ToolCall
    from tests.fakes import FakeTools

    CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    bundle_dir = tmp_path / "scout"
    agent_yaml = bundle_dir / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "tools:\n  provider: none",
            "tools:\n  provider: none\n  allow: ['files.*']",
        )
    )
    real_load = Agent.load.__func__

    def load_with_fakes(cls, path, **overrides):
        overrides.setdefault(
            "llm",
            ScriptedLLM(
                LLMResponse(
                    text="", tool_calls=[ToolCall(id="1", name="files__read", arguments={})]
                ),
                "all done",
            ),
        )
        overrides.setdefault(
            "tools", FakeTools([{"name": "files.read", "description": "", "parameters": {}}])
        )
        return real_load(cls, path, **overrides)

    monkeypatch.setattr(Agent, "load", classmethod(load_with_fakes))
    result = CliRunner().invoke(main, ["run", str(bundle_dir), "-m", "read it", "-v"])
    assert result.exit_code == 0, result.output
    assert "[tool] → files.read" in result.output
    assert "[tool] ←" in result.output
    assert "all done" in result.output


def test_run_on_a_non_bundle_is_a_clean_error(tmp_path):
    result = CliRunner().invoke(main, ["run", str(tmp_path), "-m", "hi"])
    assert result.exit_code != 0
    assert "agent.yaml" in result.output


def test_the_gate_renderer_ignores_events_that_merely_LOOK_like_it():
    """#49 r2 pre-pass: a suffix match swallowed two strangers.

    `kind.endswith(("_allowed", "_refused"))` sits after the exact-match arms
    in `_print_event`, so it inherited everything they did not claim —
    `system_task_refused` (carries `marker`) and a SECOND, pre-existing
    `tool_refused` at agent.py:557 (carries `error`). Neither is an operator
    gate and neither has a `sub`, so both rendered as `for sub=None`: a
    confident-looking line about an authorization decision that never happened.

    `tool_refused` is the reason the `sub` test is load-bearing rather than
    defensive — two call sites share that name with different payloads, so the
    name alone cannot tell them apart.
    """
    from click.testing import CliRunner

    from daedalus.cli import _print_event

    def rendered(event: dict) -> str:
        runner = CliRunner()
        with runner.isolation() as (out, _err, _):
            _print_event(event)
            return out.getvalue().decode()

    assert "teach" in rendered({"type": "teach_allowed", "sub": "service:room"})
    assert "persona:scout" in rendered({"type": "tool_refused", "sub": "persona:scout"})

    assert rendered({"type": "system_task_refused", "marker": "bogus"}) == ""
    assert rendered({"type": "tool_refused", "error": "boom"}) == ""


# Lives here, NOT in test_agent_teach.py where it was first written: that
# module is behind a techne `importorskip`, so a CLI rendering test placed
# there would never run in CI at all (daedalus#48). Nothing about this test
# needs techne.


def test_run_recall_sets_the_recall_query(tmp_path, monkeypatch):
    CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    real_handle = Agent.handle_task
    seen = []

    async def spy(self, task):
        seen.append(task.metadata.get(RECALL_KEY))
        return await real_handle(self, task)

    real_load = Agent.load.__func__

    def load_with_fake_llm(cls, path, **overrides):
        overrides.setdefault("llm", ScriptedLLM("ok"))
        return real_load(cls, path, **overrides)

    monkeypatch.setattr(Agent, "load", classmethod(load_with_fake_llm))
    monkeypatch.setattr(Agent, "handle_task", spy)
    args = ["run", str(tmp_path / "scout"), "-m", "big brief", "--recall", "title paths"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert seen == ["title paths"]


def test_run_recall_without_message_is_refused(tmp_path):
    CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    result = CliRunner().invoke(main, ["run", str(tmp_path / "scout"), "--recall", "x"])
    assert result.exit_code == 2
    assert "pass -m too" in result.output


# -- the [usage] line (daedalus#135) ----------------------------------------------


def test_usage_line_format_with_full_numbers():
    from daedalus.cli import _usage_line

    line = _usage_line({
        "type": "engine_finished", "engine": "claude-code", "models": ["claude-opus-5-5"],
        "turns": 42, "input_tokens": 1_234_567, "output_tokens": 11_234,
        "cache_read_input_tokens": 1_200_000, "cache_creation_input_tokens": 30_000,
        "total_cost_usd": 1.234,
    })
    assert line == (
        "[usage] claude-code claude-opus-5-5: 1,234,567 tokens in "
        "(1,200,000 cache read, 30,000 cache write), 11,234 out, 42 turns, "
        "~$1.23 at list price"
    )


def test_usage_line_omits_tokens_and_cost_when_absent_rather_than_print_zeros():
    from daedalus.cli import _usage_line

    line = _usage_line({
        "type": "engine_finished", "engine": "claude-code", "models": [], "turns": 2,
        "model_usage": {},
    })
    assert line == "[usage] claude-code (model unknown): 2 turns"
    assert "0" not in line  # nothing was measured, so nothing claims to be a measured 0


def test_usage_line_is_none_when_theres_nothing_to_report():
    from daedalus.cli import _usage_line

    assert _usage_line({"type": "engine_finished", "engine": "claude-code", "models": [],
                        "turns": None}) is None


def test_run_prints_the_usage_line_without_dash_v(tmp_path, monkeypatch):
    CliRunner().invoke(main, ["create", "scout", "--dir", str(tmp_path)])
    real_handle = Agent.handle_task

    async def handle_with_usage_event(self, task):
        self.on_event({
            "type": "engine_finished", "engine": "claude-code", "models": ["claude-opus-5-5"],
            "turns": 3, "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0,
            "total_cost_usd": 0.01,
        })
        return await real_handle(self, task)

    real_load = Agent.load.__func__

    def load_with_fake_llm(cls, path, **overrides):
        overrides.setdefault("llm", ScriptedLLM("ok"))
        return real_load(cls, path, **overrides)

    monkeypatch.setattr(Agent, "load", classmethod(load_with_fake_llm))
    monkeypatch.setattr(Agent, "handle_task", handle_with_usage_event)
    result = CliRunner().invoke(main, ["run", str(tmp_path / "scout"), "-m", "hi"])
    assert result.exit_code == 0, result.output
    assert "[usage] claude-code claude-opus-5-5:" in result.stderr
    assert "100 tokens in (5 cache read, 0 cache write), 20 out, 3 turns" in result.stderr
    assert "~$0.01 at list price" in result.stderr
