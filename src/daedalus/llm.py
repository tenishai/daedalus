"""LiteLLM-backed model invocation. Route strings only — no provider SDKs in core."""

from __future__ import annotations

from daedalus.protocols import LLMResponse


class LLMError(RuntimeError):
    pass


class LiteLLMClient:
    def __init__(self, model: str, *, max_tokens: int = 8192):
        self.model = model
        self.max_tokens = max_tokens

    async def complete(self, messages: list[dict]) -> LLMResponse:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover
            raise LLMError("litellm is not installed — pip install daedalus") from exc

        try:
            response = await litellm.acompletion(
                model=self.model, messages=messages, max_tokens=self.max_tokens
            )
        except Exception as exc:
            raise LLMError(f"{self.model}: {exc}") from exc

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=response.choices[0].message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
