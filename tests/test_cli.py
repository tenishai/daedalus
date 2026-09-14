from click.testing import CliRunner

import daedalus
from daedalus.agent import Agent
from daedalus.bundle import Bundle
from daedalus.cli import main
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
