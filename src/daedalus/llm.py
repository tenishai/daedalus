"""LiteLLM-backed model invocation. Route strings only — no provider SDKs in core."""

from __future__ import annotations

from daedalus.protocols import LLMResponse


class LLMError(RuntimeError):
    pass


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


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
    def __init__(self, model: str, *, max_tokens: int = 8192, api_base: str | None = None):
        self.model = model
        self.max_tokens = max_tokens
        self.api_base = api_base

    async def complete(self, messages: list[dict]) -> LLMResponse:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover
            raise LLMError("litellm is not installed — pip install daedalus") from exc

        kwargs: dict = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise LLMError(f"{self.model}: {exc}") from exc

        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", None) or ""
        text = message.content or ""
        if not reasoning:
            reasoning, text = split_reasoning(text)

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            reasoning=reasoning,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
