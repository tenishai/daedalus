import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from daedalus.bundle import AgentSpec, Bundle, BundleError, interpolate

GOLDEN = Path(__file__).parent / "golden" / "greeter"
DIARIST = Path(__file__).parent / "golden" / "diarist"


def test_golden_bundle_loads(monkeypatch):
    monkeypatch.delenv("GREETER_MODEL", raising=False)
    bundle = Bundle.load(GOLDEN)
    spec = bundle.spec
    assert spec.name == "greeter"
    assert spec.model.id == "anthropic/claude-sonnet-4-5"  # ${VAR:-default} default path
    assert spec.model.max_tokens == 1024
    assert spec.model.thinking.mode == "auto"
    assert spec.model.thinking.budget_tokens == 2048
    assert spec.model.request_timeout == 60
    assert spec.task_timeout == 300
    assert spec.model.budget.per_task_tokens == 50000
    assert [t.kind for t in spec.transports] == ["cli"]
    assert spec.tools.provider == "none"
    assert spec.memory.personal == "none"
    assert spec.approvals.ceiling.write is False
    assert spec.approvals.gates[0].match == "files.write"
    assert spec.approvals.escalation.route == "cli"
    assert bundle.card["name"] == "greeter"
    assert "Greeter" in bundle.persona


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("GREETER_MODEL", "openai/gpt-4o-mini")
    assert Bundle.load(GOLDEN).spec.model.id == "openai/gpt-4o-mini"


def test_golden_bundle_round_trips(monkeypatch):
    """load -> serialize -> load again -> identical spec."""
    monkeypatch.delenv("GREETER_MODEL", raising=False)
    spec = Bundle.load(GOLDEN).spec
    assert AgentSpec.model_validate(spec.model_dump()) == spec


def test_diarist_golden_names_its_diary_and_round_trips(monkeypatch):
    """#265: the worked example of a specialist seeded from a transcript diary."""
    monkeypatch.delenv("DIARIST_MODEL", raising=False)
    bundle = Bundle.load(DIARIST)
    spec = bundle.spec
    assert spec.name == "diarist"
    assert spec.memory.personal == "techne"
    assert spec.memory.home == "/techne"
    assert spec.memory.recall_sources == ["daffyne"]
    assert "Never speak a diary row as your own" in bundle.persona
    assert AgentSpec.model_validate(spec.model_dump()) == spec


def test_undefined_variable_without_default_fails_loud():
    with pytest.raises(BundleError, match="NO_SUCH_VAR"):
        interpolate("model: ${NO_SUCH_VAR}", env={})


def test_interpolation_walks_nested_structures():
    value = {"a": ["${X:-fallback}"], "b": {"c": "${X:-other}"}, "d": 7}
    assert interpolate(value, env={}) == {"a": ["fallback"], "b": {"c": "other"}, "d": 7}


def test_persona_html_comments_never_reach_the_model(tmp_path):
    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: m\n")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text(
        "You are x.\n\n<!-- author note:\ndelete me -->\n\nBe brief.\n"
    )
    persona = Bundle.load(tmp_path).persona
    assert "author note" not in persona
    assert "You are x." in persona and "Be brief." in persona


def test_scaffolded_persona_is_a_working_prompt(tmp_path):
    """The fresh-nib rule: a new bundle must not instruct the model to
    'describe this agent' — that text gets executed (dogfood finding #4)."""
    from daedalus.scaffold import create_bundle

    create_bundle("fresh", tmp_path)
    persona = Bundle.load(tmp_path / "fresh").persona
    assert "<!--" not in persona
    assert "Describe" not in persona
    assert "You are fresh" in persona


def test_missing_bundle_dir_is_a_bundle_error(tmp_path):
    with pytest.raises(BundleError, match="agent.yaml"):
        Bundle.load(tmp_path / "nope")


def test_missing_persona_is_a_bundle_error(tmp_path):
    (tmp_path / "agent.yaml").write_text("name: x\nmodel:\n  id: m\n")
    (tmp_path / "agent-card.json").write_text("{}")
    with pytest.raises(BundleError, match="persona.md"):
        Bundle.load(tmp_path)


def test_unknown_top_level_field_warns_but_loads(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\nfrom_the_future: true\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bundle = Bundle.load(tmp_path)
    assert bundle.spec.name == "x"
    assert any("from_the_future" in str(w.message) for w in caught)


def test_unknown_nested_field_warns_too(tmp_path):
    """Finding #11: a trace line pasted into tools: was silently ignored.
    Nested sections warn on unknown keys like the top level does."""
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  offering: git.git_status\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Bundle.load(tmp_path)
    assert any("offering" in str(w.message) for w in caught)


def test_transport_extras_are_options_not_warnings(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n  - kind: slack\n    channel: '#ops'\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("persona")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bundle = Bundle.load(tmp_path)
    assert bundle.spec.transports[0].options() == {"channel": "#ops"}
    assert not caught


def test_memory_recall_sources_default_empty_and_take_names():
    """#265: a bundle names the installed recall sources its store may consult."""
    base = {"name": "x", "card": "c.json", "persona": "p.md", "model": {"id": "m"}}
    assert AgentSpec.model_validate(base).memory.recall_sources == []
    spec = AgentSpec.model_validate(
        {**base, "memory": {"personal": "techne", "recall_sources": ["daffyne"]}}
    )
    assert spec.memory.recall_sources == ["daffyne"]
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**base, "memory": {"recall_sources": "daffyne"}})


def test_memory_team_outbox_is_rejected_with_a_clear_message():
    """#111: `team: outbox` validated and did nothing, because the team sync
    outbox was never built. It must fail loudly; `none` (the default) stays fine."""
    base = {"name": "x", "model": {"id": "m"}}
    assert AgentSpec.model_validate({**base, "memory": {"team": "none"}}).memory.team == "none"
    assert AgentSpec.model_validate(base).memory.team == "none"

    with pytest.raises(ValueError, match="team: outbox is not implemented"):
        AgentSpec.model_validate({**base, "memory": {"team": "outbox"}})
    with pytest.raises(ValueError):
        AgentSpec.model_validate({**base, "memory": {"team": "whatever"}})
