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
