"""LiteLLM-backed model invocation. Route strings only — no provider SDKs in core."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from daedalus.protocols import LLMResponse, ToolCall

if TYPE_CHECKING:
    from daedalus.bundle import SamplingSpec, ThinkingSpec

log = logging.getLogger(__name__)


def _count(usage, name: str) -> int | None:
    """A usage count as reported, or None when the provider left it out (#268).
    Not `or 0`: an unreported count must not read as zero."""
    value = getattr(usage, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class LLMError(RuntimeError):
    pass


# --- thinking, apart from the visible reply (tenishai/daedalus#240) -----------------
#
# Every provider-specific fact about thinking lives in this one table. A row names the
# routes it covers as (prefix, needle-or-None) pairs, first match wins, and the comment
# says what each mode sends; `thinking_kwargs` below reads it and nothing else does.
# Routes no row covers are sent nothing. The provider prefix decides, so a Claude served
# through an OpenAI-compatible proxy (`openai/claude-...`) takes the openai row.
#
#   anthropic   `anthropic/`, `vertex_ai/claude*`, `bedrock/anthropic*`, bare `claude*`.
#               on (and auto on a known reasoning model, Claude 3.7 or later): litellm's
#               `thinking={"type": "enabled", "budget_tokens": N}`, and the request's
#               max_tokens raised by N so the answer keeps its own budget. The pinned
#               litellm (1.104.2) translates that shape for the models that no longer
#               take a budget (4.7 and the 5 families: adaptive thinking plus an effort
#               level derived from N, 1024 low / 2048 medium / 4096 high / 8192 xhigh),
#               and forwards it as is to the models that do. off: nothing is sent, which
#               is "no thinking" on models that think only when asked, and the model's
#               default on those that always think (`disabled` is a 400 there, and the
#               vertex route does not drop it for us).
#   openai      `openai/`, `hosted_vllm/`: OpenAI-compatible servers in front of Qwen-style
#               models, which read `chat_template_kwargs.enable_thinking` from the body.
#               off sends False, on sends True (max_tokens raised by N, since vLLM counts
#               the think-trace against it), auto sends nothing: whether the model
#               reasons at all is unknown from its id.
_THINKING_ROUTES: tuple[tuple[str, tuple[tuple[str, str | None], ...]], ...] = (
    (
        "anthropic",
        (
            ("anthropic/", None),
            ("claude", None),
            ("vertex_ai/", "claude"),
            ("bedrock/", "anthropic"),
        ),
    ),
    ("openai", (("openai/", None), ("hosted_vllm/", None))),
)

#: Thinking tokens allowed on top of max_tokens when a bundle or tier names none.
DEFAULT_THINKING_BUDGET = 4096

#: The first Claude generation that thinks on request (3.7); everything from there on
#: is a reasoning model, so `auto` asks it to think with a budget of its own.
_FIRST_REASONING_CLAUDE = (3, 7)
_CLAUDE_VERSION = re.compile(r"claude-(?:[a-z]+-)*(\d+)(?:-(\d+))?")


def _thinking_family(model: str) -> str | None:
    lowered = model.lower()
    for family, routes in _THINKING_ROUTES:
        for prefix, needle in routes:
            if lowered.startswith(prefix) and (needle is None or needle in lowered):
                return family
    return None


def is_reasoning_claude(model: str) -> bool:
    """Whether a Claude route names a generation that thinks (3.7 and later). An id
    with no version in it is unknown, so `auto` leaves it alone."""
    found = _CLAUDE_VERSION.search(model.lower())
    if not found:
        return False
    major, minor = int(found.group(1)), int(found.group(2) or 0)
    return (major, minor) >= _FIRST_REASONING_CLAUDE


def thinking_kwargs(
    model: str, spec: ThinkingSpec | None, max_tokens: int
) -> tuple[dict, int]:
    """The litellm kwargs for `spec` on `model`, and the max_tokens to send.

    Returns ({}, max_tokens) whenever nothing is asked for: routes the table above
    does not know, `auto` on a model whose reasoning is unknown, and `off` where the
    route has no way to say so. Pure, so the contract tests read it directly.
    """
    mode = spec.mode if spec is not None else "auto"
    budget = (spec.budget_tokens if spec is not None else None) or DEFAULT_THINKING_BUDGET
    family = _thinking_family(model)
    if family == "anthropic":
        if mode == "on" or (mode == "auto" and is_reasoning_claude(model)):
            return (
                {"thinking": {"type": "enabled", "budget_tokens": budget}},
                max_tokens + budget,
            )
        return {}, max_tokens
    if family == "openai":
        if mode == "off":
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}, max_tokens
        if mode == "on":
            return (
                {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
                max_tokens + budget,
            )
    return {}, max_tokens


#: Sampling fields litellm passes as top-level kwargs on every route it maps them for.
_TOP_LEVEL_SAMPLING = ("temperature", "top_p", "presence_penalty", "frequency_penalty")
#: Fields litellm's OpenAI-compatible route drops at top level; they ride in extra_body,
#: which vLLM, llama.cpp, SGLang and OpenRouter read.
_BODY_SAMPLING = ("top_k", "min_p", "repetition_penalty")


def sampling_kwargs(
    model: str,
    sampling: SamplingSpec | None,
    by_thinking: dict[str, SamplingSpec] | None,
    thinking: ThinkingSpec | None,
) -> dict:
    """The litellm kwargs for a model's sampling: `sampling`, then the entry for an
    explicit thinking mode on top, field by field (after pi's resolveSamplingParams,
    packages/ai/src/api/simple-options.ts). Unset fields are not sent, so nothing set
    means {} and the request is byte-for-byte what it was before sampling existed.

    Claude routes refuse it: the Claude API rejects temperature, top_p and top_k on
    current models (and with extended thinking on older ones), so a bundle setting
    them would fail on every call. Refusing at load says so once, where it can be
    fixed, instead of 400s at run time."""
    merged: dict = {}
    for spec in (sampling, (by_thinking or {}).get(thinking.mode) if thinking else None):
        if spec is not None:
            merged.update(spec.model_dump(exclude_none=True))
    if not merged:
        return {}
    if _thinking_family(model) == "anthropic":
        from daedalus.bundle import BundleError

        raise BundleError(
            f"model.sampling sets {', '.join(sorted(merged))} for {model}, and the Claude "
            "API rejects sampling parameters on current models; remove model.sampling "
            "for Claude routes"
        )
    kwargs = {k: merged[k] for k in _TOP_LEVEL_SAMPLING if k in merged}
    body = {k: merged[k] for k in _BODY_SAMPLING if k in merged}
    if body:
        kwargs["extra_body"] = body
    return kwargs


def _merge_kwargs(base: dict, extra: dict) -> dict:
    """`base` with `extra` laid over it; the two extra_body dicts are combined, not
    replaced, so thinking's chat_template_kwargs and sampling's top_k both go."""
    merged = {**base, **extra}
    if "extra_body" in base and "extra_body" in extra:
        merged["extra_body"] = {**base["extra_body"], **extra["extra_body"]}
    return merged


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


def cache_marked(messages: list[dict], model: str) -> list[dict]:
    """Anthropic prompt-cache breakpoint on the system message.

    The system block (persona + recalled context + notices) plus the tool
    schemas are the stable prefix an agent re-sends on EVERY turn of every
    task -- live traces showed them costing ~20k tokens per turn at full
    price. Anthropic caches everything up to a cache_control marker, and
    the provider orders tools BEFORE system in the prefix, so one marker
    on the system content covers both. Cache reads bill at a tenth of
    fresh input; within a task every turn after the first hits.

    Only claude-family routes understand the marker (direct, vertex_ai/,
    bedrock/); every other model gets the messages back untouched. The
    original list is never mutated -- callers may reuse it.
    """
    if "claude" not in model.lower():
        return messages
    if not messages or messages[0].get("role") != "system":
        return messages
    content = messages[0].get("content")
    if not isinstance(content, str):
        return messages
    marked = dict(messages[0])
    marked["content"] = [
        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
    ]
    return [marked, *messages[1:]]


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 8192,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        thinking: ThinkingSpec | None = None,
        sampling: SamplingSpec | None = None,
        sampling_by_thinking: dict[str, SamplingSpec] | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        # What the provider is told about thinking, and the max_tokens that goes with
        # it (#240): fixed per client, like everything else here.
        self.thinking_kwargs, self.max_tokens_sent = thinking_kwargs(model, thinking, max_tokens)
        # Fixed per client too; {} when the bundle sets no sampling.
        self.sampling_kwargs = sampling_kwargs(model, sampling, sampling_by_thinking, thinking)
        # Seconds per call (#201). None leaves litellm's own default, 6000 s.
        self.timeout = timeout
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
            raise LLMError("litellm is not installed — pip install tenish-daedalus") from exc

        kwargs: dict = _merge_kwargs(
            {
                "model": self.model,
                "messages": cache_marked(messages, self.model),
                "max_tokens": self.max_tokens_sent,
                **self.thinking_kwargs,
            },
            self.sampling_kwargs,
        )
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if tools:
            kwargs["tools"] = tools
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
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
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        completion_tokens = _count(usage, "completion_tokens")
        if finish_reason == "length":
            # The one line that makes a cut reply diagnosable from the agent's log
            # (#240): did the answer run long, or did thinking eat the budget first?
            # The reply itself still gets the engine's truncated sentence.
            details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            log.info(
                "reply truncated: model=%s max_tokens_sent=%d completion_tokens=%s "
                "reasoning_tokens=%s thinking=%s",
                self.model,
                self.max_tokens_sent,
                completion_tokens if completion_tokens is not None else "unreported",
                reasoning_tokens if reasoning_tokens is not None else "unreported",
                self.thinking_kwargs.get("thinking")
                or self.thinking_kwargs.get("extra_body")
                or "not sent",
            )
        return LLMResponse(
            text=text,
            reasoning=reasoning,
            tool_calls=tool_calls,
            input_tokens=_count(usage, "prompt_tokens"),
            output_tokens=completion_tokens,
            # litellm carries cache counts in prompt_tokens_details, and for Anthropic
            # its prompt_tokens already INCLUDES them (anthropic/chat/transformation.py,
            # calculate_usage), so these are a breakdown, not an addition.
            cache_read_input_tokens=_count(
                getattr(usage, "prompt_tokens_details", None), "cached_tokens"
            ),
            cache_creation_input_tokens=_count(
                getattr(usage, "prompt_tokens_details", None), "cache_write_tokens"
            ),
            finish_reason=finish_reason,
        )
