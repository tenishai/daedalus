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
