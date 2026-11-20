from daedalus.llm import split_reasoning
from daedalus.protocols import LLMResponse


def test_split_reasoning_with_full_tags():
    reasoning, answer = split_reasoning("<think>step 1, step 2</think>\n\nHello!")
    assert reasoning == "step 1, step 2"
    assert answer == "Hello!"


def test_split_reasoning_when_template_ate_the_opening_tag():
    # vLLM's R1 chat template consumes <think>; only the close tag survives
    reasoning, answer = split_reasoning("Okay, the user greeted me...\n</think>\n\nHi there!")
    assert reasoning.startswith("Okay, the user")
    assert answer == "Hi there!"


def test_split_reasoning_passes_plain_text_through():
    assert split_reasoning("just an answer") == ("", "just an answer")


def test_llm_response_reasoning_defaults_empty():
    assert LLMResponse(text="hi").reasoning == ""


def test_strip_special_tokens_removes_chatml_leakage():
    from daedalus.llm import strip_special_tokens

    spew = "answer here\n<|im_start|>\nmore\n<|im_end|><|endoftext|>"
    assert strip_special_tokens(spew) == "answer here\n\nmore"


def test_strip_special_tokens_leaves_normal_text_alone():
    from daedalus.llm import strip_special_tokens

    text = "a | b, x || y, and a <tag> too"
    assert strip_special_tokens(text) == text


# --- prompt-cache markers (2026-09-05, the per-turn context floor) ------------


def test_cache_marked_wraps_system_for_claude_routes():
    from daedalus.llm import cache_marked

    messages = [
        {"role": "system", "content": "You are x."},
        {"role": "user", "content": "hi"},
    ]
    out = cache_marked(messages, "vertex_ai/claude-sonnet-4-5")
    assert out[0]["content"] == [
        {"type": "text", "text": "You are x.", "cache_control": {"type": "ephemeral"}}
    ]
    assert out[1] == {"role": "user", "content": "hi"}
    # The caller's list is untouched -- the loop reuses it across turns.
    assert messages[0]["content"] == "You are x."


def test_cache_marked_leaves_other_models_alone():
    from daedalus.llm import cache_marked

    messages = [{"role": "system", "content": "You are x."}]
    assert cache_marked(messages, "openai/qwen-coder") is messages


def test_cache_marked_handles_missing_or_odd_system_gracefully():
    from daedalus.llm import cache_marked

    no_system = [{"role": "user", "content": "hi"}]
    assert cache_marked(no_system, "claude-sonnet-4-5") is no_system
    already_blocks = [{"role": "system", "content": [{"type": "text", "text": "x"}]}]
    assert cache_marked(already_blocks, "claude-sonnet-4-5") is already_blocks


# --- thinking apart from the visible reply (tenishai/daedalus#240) -------------------
#
# A fake `litellm.acompletion` records the kwargs daedalus sends; nothing reaches the
# network. The provider table in daedalus.llm is the thing under test.

import logging  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from daedalus.bundle import ThinkingSpec  # noqa: E402
from daedalus.llm import DEFAULT_THINKING_BUDGET, LiteLLMClient, thinking_kwargs  # noqa: E402


def _response(finish_reason="stop", completion_tokens=5, reasoning_tokens=None):
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens)
        if reasoning_tokens is not None
        else None
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="X=1", reasoning_content=None, tool_calls=None),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=completion_tokens,
            completion_tokens_details=details,
        ),
    )


@pytest.fixture
def sent(monkeypatch):
    """Patch litellm.acompletion; yields the list of kwargs dicts it was called with."""
    import litellm

    class Sent(list):
        reply = {"response": _response()}

    calls = Sent()

    async def fake(**kwargs):
        calls.append(kwargs)
        return calls.reply["response"]

    monkeypatch.setattr(litellm, "acompletion", fake)
    return calls


async def test_an_anthropic_model_with_thinking_on_sends_the_budget_and_a_raised_ceiling(sent):
    client = LiteLLMClient(
        "anthropic/claude-sonnet-5-5",
        max_tokens=8192,
        api_key="k",
        thinking=ThinkingSpec(mode="on", budget_tokens=2048),
    )
    await client.complete([{"role": "user", "content": "one line"}])
    (kwargs,) = sent
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert kwargs["max_tokens"] == 8192 + 2048  # the answer keeps its whole budget
    assert "extra_body" not in kwargs


async def test_an_anthropic_model_with_thinking_off_sends_neither(sent):
    client = LiteLLMClient(
        "anthropic/claude-sonnet-5-5", max_tokens=8192, api_key="k",
        thinking=ThinkingSpec(mode="off"),
    )
    await client.complete([{"role": "user", "content": "one line"}])
    (kwargs,) = sent
    assert "thinking" not in kwargs and "extra_body" not in kwargs
    assert kwargs["max_tokens"] == 8192


def test_auto_asks_a_reasoning_claude_to_think_and_leaves_older_ones_alone():
    # No spec at all is `auto` with the default budget: the shape every bundle gets.
    for route in (
        "anthropic/claude-sonnet-5-5",
        "vertex_ai/claude-sonnet-4-5",
        "bedrock/anthropic.claude-opus-5-5-v1:0",
        "claude-3-7-sonnet-20250219",
    ):
        extra, ceiling = thinking_kwargs(route, None, 8192)
        assert extra == {"thinking": {"type": "enabled", "budget_tokens": DEFAULT_THINKING_BUDGET}}
        assert ceiling == 8192 + DEFAULT_THINKING_BUDGET
    for route in ("anthropic/claude-3-5-sonnet", "claude-3-haiku-20240307", "claude-instant-1"):
        assert thinking_kwargs(route, None, 8192) == ({}, 8192)


async def test_an_openai_compatible_model_with_thinking_off_sends_the_template_switch(sent):
    client = LiteLLMClient(
        "openai/qwen3-32b", max_tokens=8192, api_base="http://host:8000/v1",
        thinking=ThinkingSpec(mode="off"),
    )
    await client.complete([{"role": "user", "content": "one line"}])
    (kwargs,) = sent
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "thinking" not in kwargs
    assert kwargs["max_tokens"] == 8192


def test_openai_compatible_on_and_auto():
    on, ceiling = thinking_kwargs("hosted_vllm/qwen3", ThinkingSpec(mode="on"), 8192)
    assert on == {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    assert ceiling == 8192 + DEFAULT_THINKING_BUDGET
    # auto: whether the model behind the proxy reasons is unknown from its id
    assert thinking_kwargs("openai/qwen3", None, 8192) == ({}, 8192)
    assert thinking_kwargs("openai/qwen3", ThinkingSpec(mode="auto"), 8192) == ({}, 8192)


def test_the_provider_prefix_decides_the_row_not_the_model_name():
    # A Claude behind an OpenAI-compatible proxy speaks that proxy's dialect: litellm's
    # openai path has no `thinking` kwarg, so the openai row applies.
    assert thinking_kwargs("openai/claude-sonnet-5-5", None, 8192) == ({}, 8192)
    off, _ = thinking_kwargs("openai/claude-sonnet-5-5", ThinkingSpec(mode="off"), 8192)
    assert off == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    # and the cross-region bedrock id form still lands on the anthropic row
    extra, _ = thinking_kwargs("bedrock/us.anthropic.claude-sonnet-5-5-v1:0", None, 8192)
    assert "thinking" in extra


def test_routes_the_table_does_not_know_are_sent_nothing():
    for mode in ("auto", "on", "off"):
        assert thinking_kwargs("gemini/gemini-2.5-pro", ThinkingSpec(mode=mode), 8192) == ({}, 8192)


async def test_a_length_finish_reason_logs_the_counts(sent, caplog):
    sent.reply["response"] = _response(
        finish_reason="length", completion_tokens=12288, reasoning_tokens=11900
    )
    client = LiteLLMClient("anthropic/claude-sonnet-5-5", max_tokens=8192, api_key="k")
    with caplog.at_level(logging.INFO, logger="daedalus.llm"):
        response = await client.complete([{"role": "user", "content": "one line"}])
    assert response.finish_reason == "length"  # the engine's sentence still follows
    (record,) = [r for r in caplog.records if "reply truncated" in r.getMessage()]
    line = record.getMessage()
    assert "model=anthropic/claude-sonnet-5-5" in line
    assert f"max_tokens_sent={8192 + DEFAULT_THINKING_BUDGET}" in line
    assert "completion_tokens=12288" in line
    assert "reasoning_tokens=11900" in line


async def test_a_length_finish_reason_without_a_reasoning_count_says_so(sent, caplog):
    sent.reply["response"] = _response(finish_reason="length", completion_tokens=8192)
    client = LiteLLMClient("openai/qwen3", max_tokens=8192, api_base="http://h/v1")
    with caplog.at_level(logging.INFO, logger="daedalus.llm"):
        await client.complete([{"role": "user", "content": "one line"}])
    (record,) = [r for r in caplog.records if "reply truncated" in r.getMessage()]
    assert "reasoning_tokens=unreported" in record.getMessage()
    assert "thinking=not sent" in record.getMessage()


async def test_a_finished_reply_logs_nothing(sent, caplog):
    client = LiteLLMClient("anthropic/claude-sonnet-5-5", max_tokens=8192, api_key="k")
    with caplog.at_level(logging.INFO, logger="daedalus.llm"):
        await client.complete([{"role": "user", "content": "one line"}])
    assert not [r for r in caplog.records if "reply truncated" in r.getMessage()]


# ---- usage is reported honestly (#268) ------------------------------------


async def test_missing_usage_is_none_not_zero(sent):
    response = _response()
    response.usage = None
    sent.reply["response"] = response
    result = await LiteLLMClient("openai/gpt-x").complete([{"role": "user", "content": "q"}])
    assert result.input_tokens is None and result.output_tokens is None
    assert result.cache_read_input_tokens is None
    assert result.tokens_spent() == 0


async def test_cache_counts_come_from_prompt_tokens_details(sent):
    response = _response()
    response.usage.prompt_tokens_details = SimpleNamespace(
        cached_tokens=700, cache_write_tokens=200
    )
    sent.reply["response"] = response
    result = await LiteLLMClient("openai/gpt-x").complete([{"role": "user", "content": "q"}])
    assert result.input_tokens == 10
    assert result.cache_read_input_tokens == 700
    assert result.cache_creation_input_tokens == 200
