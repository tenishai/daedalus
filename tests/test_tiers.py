"""Model tiers (#103): a bundle asks for cheap, medium or high; the deployment's tiers
file says which model that is. Each rule in daedalus/tiers.py has a case here."""

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle, BundleError
from daedalus.tiers import TierError, load_tiers
from tests.fakes import ScriptedLLM

TIERS = """
tiers:
  high: {engine: claude-code, id: claude-opus-5-5}
  medium:
    engine: litellm
    id: openai/local-9b
    api_base: http://10.0.0.5:8000/v1
    api_key_env: LOCAL_KEY
    context_tokens: 45000
    max_repeats: 3
  cheap: medium
"""


def bundle(tmp_path, model_yaml: str, extra: str = "") -> Bundle:
    (tmp_path / "agent.yaml").write_text(f"name: x\nmodel:\n{model_yaml}{extra}")
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    return Bundle.load(tmp_path)


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    path = tmp_path / "tiers.yaml"
    path.write_text(TIERS)
    monkeypatch.setenv("DAEDALUS_TIERS", str(path))
    monkeypatch.delenv("DAEDALUS_ENGINE", raising=False)
    return path


def load(b: Bundle, **kw) -> Agent:
    events = []
    agent = Agent(b, llm=ScriptedLLM("ok"), on_event=events.append, **kw)
    agent.seen = events
    return agent


def test_a_tier_fills_the_model_the_endpoint_and_the_models_limits(tmp_path, tiers):
    b = bundle(tmp_path, "  tier: medium\n")
    agent = load(b)
    assert agent.engine_name == "litellm"
    s = agent.bundle.spec
    assert s.model.id == "openai/local-9b"
    assert s.model.api_base == "http://10.0.0.5:8000/v1"
    assert s.model.api_key_env == "LOCAL_KEY"
    assert s.tools.context_tokens == 45000 and s.tools.max_repeats == 3
    r = agent.model_resolution.as_dict()
    assert r == {"tier": "medium", "used": "medium", "engine": "litellm",
                 "id": "openai/local-9b", "fell_back": False, "source": str(tiers)}


def test_the_resolved_model_is_emitted_as_a_trace_event(tmp_path, tiers):
    agent = load(bundle(tmp_path, "  tier: medium\n"))
    replay, _ = agent.events.subscribe()
    assert [e["type"] for e in replay] == ["model_resolved"]
    assert replay[0]["id"] == "openai/local-9b"


def test_a_claude_code_tier_picks_the_engine_and_its_model(tmp_path, tiers):
    b = bundle(tmp_path, "  tier: high\n")
    agent = load(b)
    assert agent.engine_name == "claude-code"
    assert agent.bundle.spec.engine.claude_code.model == "claude-opus-5-5"


def test_an_explicit_engine_that_disagrees_with_the_tier_is_refused(tmp_path, tiers):
    with pytest.raises(BundleError, match="the high tier runs on claude-code"):
        load(bundle(tmp_path, "  tier: high\n"), engine="litellm")


def test_a_non_empty_id_wins_and_is_recorded_as_pinned(tmp_path, tiers):
    b = bundle(tmp_path, "  id: anthropic/claude-sonnet-5\n  tier: high\n")
    agent = load(b)
    assert agent.bundle.spec.model.id == "anthropic/claude-sonnet-5"
    assert agent.engine_name == "litellm"
    assert agent.model_resolution.source == "id"
    assert agent.model_resolution.tier == "high"
    assert agent.model_resolution.fell_back is False  # a pin is not a fallback


def test_an_empty_id_counts_as_unset_so_the_tier_applies(tmp_path, tiers, monkeypatch):
    monkeypatch.delenv("MY_MODEL", raising=False)
    b = bundle(tmp_path, '  id: "${MY_MODEL:-}"\n  tier: medium\n')
    assert load(b).bundle.spec.model.id == "openai/local-9b"


def test_values_the_bundle_set_win_over_the_tier_entry(tmp_path, tiers):
    b = bundle(tmp_path, "  tier: medium\n  api_base: http://mine/v1\n",
               "tools:\n  context_tokens: 20000\n")
    s = load(b).bundle.spec
    assert s.model.api_base == "http://mine/v1"
    assert s.tools.context_tokens == 20000
    assert s.tools.max_repeats == 3  # not set by the bundle, so the tier's


def test_an_aliased_tier_resolves_to_its_target(tmp_path, tiers):
    agent = load(bundle(tmp_path, "  tier: cheap\n"))
    assert agent.model_resolution.used == "cheap"
    assert agent.model_resolution.id == "openai/local-9b"


def test_a_missing_tier_refuses_to_load_by_default(tmp_path, monkeypatch):
    path = tmp_path / "tiers.yaml"
    path.write_text("tiers:\n  high: null\n  medium: {id: m}\n")
    monkeypatch.setenv("DAEDALUS_TIERS", str(path))
    with pytest.raises(BundleError, match="needs the high tier, and this deployment offers medium"):
        load(bundle(tmp_path, "  tier: high\n"))


def test_min_tier_lets_a_bundle_fall_back_and_says_so(tmp_path, monkeypatch):
    path = tmp_path / "tiers.yaml"
    path.write_text("tiers:\n  high: null\n  medium: {id: m}\n")
    monkeypatch.setenv("DAEDALUS_TIERS", str(path))
    agent = load(bundle(tmp_path, "  tier: high\n  min_tier: medium\n"))
    r = agent.model_resolution
    assert (r.tier, r.used, r.id, r.fell_back) == ("high", "medium", "m", True)


def test_min_tier_is_a_floor_not_a_licence_to_go_lower(tmp_path, monkeypatch):
    path = tmp_path / "tiers.yaml"
    path.write_text("tiers:\n  cheap: {id: tiny}\n")
    monkeypatch.setenv("DAEDALUS_TIERS", str(path))
    with pytest.raises(BundleError, match="or down to medium"):
        load(bundle(tmp_path, "  tier: high\n  min_tier: medium\n"))


def test_a_tier_with_no_tiers_file_says_how_to_fix_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DAEDALUS_TIERS", raising=False)
    with pytest.raises(BundleError, match="DAEDALUS_TIERS names no tiers file"):
        load(bundle(tmp_path, "  tier: medium\n"))


@pytest.mark.parametrize("model_yaml,message", [
    ("  max_tokens: 10\n", "needs an id or a tier"),
    ('  id: ""\n', "needs an id or a tier"),
    ("  id: m\n  min_tier: cheap\n", "min_tier needs model.tier"),
])
def test_the_model_block_needs_an_id_or_a_tier(tmp_path, model_yaml, message):
    with pytest.raises(Exception, match=message):
        bundle(tmp_path, model_yaml)


def test_min_tier_above_the_tier_is_refused(tmp_path, tiers):
    with pytest.raises(BundleError, match="min_tier high is above model.tier medium"):
        load(bundle(tmp_path, "  tier: medium\n  min_tier: high\n"))


@pytest.mark.parametrize("text,message", [
    ("tiers:\n  huge: {id: x}\n", "unknown tier"),
    ("tiers:\n  high: medium\n  medium: high\n", "aliases loop"),
    ("tiers:\n  high: {id: x, colour: red}\n", "tier high"),
    ("nope: 1\n", "top-level 'tiers:'"),
])
def test_a_bad_tiers_file_names_the_problem(tmp_path, text, message):
    path = tmp_path / "tiers.yaml"
    path.write_text(text)
    with pytest.raises(TierError, match=message):
        load_tiers(path)


@pytest.mark.parametrize("name", ["claude", "mixed"])
def test_the_shipped_example_tiers_files_load(name):
    # examples/tiers/local-host.yaml is a fill-in template, so only the concrete ones.
    from pathlib import Path

    table = load_tiers(Path(__file__).parent.parent / "examples" / "tiers" / f"{name}.yaml")
    assert all(table[t] is not None for t in ("cheap", "medium", "high"))
    assert table["high"].id == "claude-opus-5-5"


def test_resolving_leaves_the_bundle_unresolved_for_the_next_agent(tmp_path, tiers):
    b = bundle(tmp_path, "  tier: high\n")
    load(b)
    assert b.spec.model.id is None and b.spec.engine.claude_code.model is None
    with pytest.raises(BundleError, match="the high tier runs on claude-code"):
        load(b, engine="litellm")  # a reused bundle still gets the engine check


def test_the_trace_line_says_pinned_for_a_pinned_id(tmp_path, tiers):
    from click.testing import CliRunner

    from daedalus.cli import _print_event

    agent = load(bundle(tmp_path, "  id: m\n  tier: high\n"))
    event = next(e for e in agent.seen if e["type"] == "model_resolved")
    out = CliRunner().invoke(__import__("click").command()(lambda: _print_event(event)))
    assert "(pinned)" in out.output and "fell back" not in out.output
