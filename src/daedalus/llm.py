"""LiteLLM-backed model invocation. Route strings only — no provider SDKs in core."""

from __future__ import annotations

import json
import re

from daedalus.protocols import LLMResponse, ToolCall


class LLMError(RuntimeError):
    pass


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_SPECIAL_TOKEN = re.compile(r"<\|[^|>]{1,32}\|>")


def strip_special_tokens(text: str) -> str:
    """Remove chat-format special tokens (<|im_start|>, <|endoftext|>, ...)
    that degenerating local models leak into their output."""
    return _SPECIAL_TOKEN.sub("", text).strip()


def split_reasoning(text: str) -> tuple[str, str]:
    """Split a reasoning model's think-trace from its answer.

    Handles both ``<think>...</think>`` and the vLLM/R1 chat-template case
    where the opening tag is consumed and only ``</think>`` remains.
    Returns (reasoning, answer).
    """
    if _THINK_CLOSE not in text:
        return "", text
    reasoning, _, answer = text.rpartition(_THINK_CLOSE)
    return reasoning.replace(_THINK_OPEN, "").strip(), answer.strip()


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 8192,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.api_base = api_base
        # Local OpenAI-compatible servers ignore the key but litellm demands
        # one (finding #21: spawned agents lack the operator's shell env) —
        # a bundle with api_base must be self-contained.
        if api_key is None and api_base:
            api_key = "no-auth"
        self.api_key = api_key

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover
            raise LLMError("litellm is not installed — pip install daedalus") from exc

        kwargs: dict = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if tools:
            kwargs["tools"] = tools
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise LLMError(f"{self.model}: {exc}") from exc

        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", None) or ""
        text = message.content or ""
        if not reasoning:
            reasoning, text = split_reasoning(text)
        text = strip_special_tokens(text)

        tool_calls = []
        for raw in getattr(message, "tool_calls", None) or []:
            try:
                arguments = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": raw.function.arguments}
            tool_calls.append(ToolCall(id=raw.id, name=raw.function.name, arguments=arguments))

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            reasoning=reasoning,
            tool_calls=tool_calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )
