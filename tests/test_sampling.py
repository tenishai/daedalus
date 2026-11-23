"""model.sampling and model.sampling_by_thinking (litellm engine only).

Off by default: a bundle that sets neither sends exactly the request it sent before.
"""

import pytest

from daedalus.bundle import BundleError, SamplingSpec, ThinkingSpec
from daedalus.llm import LiteLLMClient, sampling_kwargs
from tests.test_llm import sent  # noqa: F401  (the fixture)
from tests.test_tiers import bundle, load

QWEN = "openai/qwen3.6-27b"
ON = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
OFF = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}


def by_thinking():
    return {"on": SamplingSpec(**ON), "off": SamplingSpec(**OFF)}


async def _sent_kwargs(sent, **client):  # noqa: F811
    await LiteLLMClient(QWEN, api_base="http://x/v1", **client).complete(
        [{"role": "user", "content": "hi"}]
    )
    return sent[-1]


async def test_without_sampling_the_request_is_unchanged(sent):  # noqa: F811
    before = await _sent_kwargs(sent, thinking=ThinkingSpec(mode="off"))
    after = await _sent_kwargs(
        sent, thinking=ThinkingSpec(mode="off"), sampling=None, sampling_by_thinking=None
    )
    assert before == after
    assert not {"temperature", "top_p", "presence_penalty"} & set(after)
    assert after["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


async def test_top_level_and_body_fields_go_where_litellm_passes_them(sent):  # noqa: F811
    kwargs = await _sent_kwargs(
        sent, sampling=SamplingSpec(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0)
    )
    assert kwargs["temperature"] == 0.6 and kwargs["top_p"] == 0.95
    assert kwargs["extra_body"] == {"top_k": 20, "min_p": 0.0}  # 0.0 is a value, not unset
    assert "top_k" not in kwargs


async def test_sampling_and_thinking_share_extra_body(sent):  # noqa: F811
    kwargs = await _sent_kwargs(
        sent, thinking=ThinkingSpec(mode="off"), sampling_by_thinking=by_thinking()
    )
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
    }
    assert kwargs["temperature"] == 0.7 and kwargs["presence_penalty"] == 1.5


def test_the_thinking_mode_entry_wins_over_sampling_field_by_field():
    kw = sampling_kwargs(
        QWEN,
        SamplingSpec(temperature=1.0, top_p=0.9, frequency_penalty=0.1),
        by_thinking(),
        ThinkingSpec(mode="on"),
    )
    assert kw["temperature"] == 0.6 and kw["top_p"] == 0.95  # the mode's values
    assert kw["frequency_penalty"] == 0.1  # kept from sampling: the mode leaves it unset
    assert "presence_penalty" not in kw  # the off entry is not used


@pytest.mark.parametrize("thinking", [None, ThinkingSpec(mode="auto")])
def test_auto_or_no_thinking_uses_sampling_alone(thinking):
    kw = sampling_kwargs(QWEN, SamplingSpec(temperature=0.3), by_thinking(), thinking)
    assert kw == {"temperature": 0.3}


def test_nothing_set_sends_nothing():
    assert sampling_kwargs(QWEN, None, None, ThinkingSpec(mode="on")) == {}
    assert sampling_kwargs(QWEN, SamplingSpec(), {"on": SamplingSpec()}, None) == {}


def test_a_claude_route_refuses_sampling():
    with pytest.raises(BundleError, match="Claude API rejects sampling"):
        LiteLLMClient("anthropic/claude-sonnet-5-5", sampling=SamplingSpec(temperature=0.5))
    # Nothing set is still fine on Claude.
    LiteLLMClient("anthropic/claude-sonnet-5-5", sampling=SamplingSpec())


@pytest.mark.parametrize(
    "field,value", [("temperature", 3), ("top_p", 0), ("top_k", 0), ("presence_penalty", 5)]
)
def test_out_of_range_values_are_refused(field, value):
    with pytest.raises(ValueError):
        SamplingSpec(**{field: value})


def test_a_bare_yaml_off_key_is_the_word_off(tmp_path):
    b = bundle(
        tmp_path,
        "  id: openai/x\n  sampling_by_thinking:\n    off: {temperature: 0.7}\n"
        "    on: {temperature: 0.6}\n",
    )
    assert set(b.spec.model.sampling_by_thinking) == {"on", "off"}


# --- tiers fill sampling when the bundle is silent -----------------------------------

TIERS = """
tiers:
  balanced:
    engine: litellm
    id: openai/local-27b
    api_base: http://10.0.0.5:8000/v1
    sampling: {temperature: 0.6, top_p: 0.95}
    sampling_by_thinking:
      off: {temperature: 0.7, presence_penalty: 1.5}
  fast: balanced
"""


@pytest.fixture
def sampling_tiers(tmp_path, monkeypatch):
    path = tmp_path / "tiers.yaml"
    path.write_text(TIERS)
    monkeypatch.setenv("DAEDALUS_TIERS", str(path))
    monkeypatch.delenv("DAEDALUS_ENGINE", raising=False)


def test_a_tier_fills_sampling_when_the_bundle_is_silent(tmp_path, sampling_tiers):
    m = load(bundle(tmp_path, "  id: balanced\n")).bundle.spec.model
    assert m.sampling.temperature == 0.6 and m.sampling.top_p == 0.95
    assert m.sampling_by_thinking["off"].presence_penalty == 1.5


def test_the_bundles_own_sampling_wins_over_the_tier(tmp_path, sampling_tiers):
    m = load(bundle(tmp_path, "  id: balanced\n  sampling: {temperature: 0.2}\n")).bundle.spec.model
    assert m.sampling.temperature == 0.2 and m.sampling.top_p is None  # not merged with tier
    assert m.sampling_by_thinking["off"].temperature == 0.7  # silent here, so the tier's


def test_a_claude_code_tier_may_not_carry_sampling(tmp_path, monkeypatch):
    from daedalus.tiers import TierError, load_tiers

    path = tmp_path / "tiers.yaml"
    path.write_text("tiers:\n  best: {engine: claude-code, id: m, sampling: {temperature: 1}}\n")
    with pytest.raises((TierError, ValueError), match="claude-code engine ignores it"):
        load_tiers(path)


def test_a_built_agent_passes_the_bundles_sampling_to_its_client(tmp_path, monkeypatch):
    from daedalus.agent import Agent

    monkeypatch.delenv("DAEDALUS_TIERS", raising=False)
    monkeypatch.delenv("DAEDALUS_ENGINE", raising=False)
    b = bundle(
        tmp_path,
        f"  id: {QWEN}\n  api_base: http://x/v1\n  sampling: {{temperature: 0.6, top_k: 20}}\n",
    )
    agent = Agent(b)
    assert agent.llm.sampling_kwargs == {"temperature": 0.6, "extra_body": {"top_k": 20}}
