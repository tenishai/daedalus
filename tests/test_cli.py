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


def test_run_on_a_non_bundle_is_a_clean_error(tmp_path):
    result = CliRunner().invoke(main, ["run", str(tmp_path), "-m", "hi"])
    assert result.exit_code != 0
    assert "agent.yaml" in result.output
