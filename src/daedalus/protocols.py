"""Protocol seams and their no-op defaults.

Every cross-cutting concern is a Protocol with a no-op default injected at
wiring time — never None-checked in the hot path. Real backends are chosen by
agent.yaml and imported lazily; their packages come from extras (techne, slack)
or a separate install (liminal). There are no entry points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from daedalus.task import Task


@dataclass
class ToolCall:
    """A model's request to invoke a tool. `name` is exactly what the model
    said — mapping it back to a real tool is the runtime's job, so a
    hallucinated name misses the map instead of crashing."""

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str
    reasoning: str = ""  # think-trace from reasoning models; never part of the reply
    tool_calls: list[ToolCall] = field(default_factory=list)
    # None = the provider did not report it (#268); never read a missing count as 0.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    finish_reason: str | None = None  # "length" = truncated by max_tokens (finding #27)

    def tokens_spent(self) -> int:
        """Input plus output for a token budget, counting an unreported figure
        as 0: a budget can only spend what was reported."""
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def usage_fields(self) -> dict:
        """The four counts, for a `usage` trace event."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@runtime_checkable
class LLM(Protocol):
    """Model, max_tokens, etc. are construction-time config, not call args.

    `tools`, when given, is OpenAI function-tool shaped:
    [{"type": "function", "function": {"name", "description", "parameters"}}]
    """

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse: ...


@runtime_checkable
class Handler(Protocol):
    """The entire agent contract."""

    async def handle_task(self, task: Task) -> Task: ...


@runtime_checkable
class Transport(Protocol):
    """Turns an external medium into Tasks and renders updates back."""

    async def serve(self, handler: Handler) -> None: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Optional, so never annotated here (an annotated member is required by
    isinstance): `recall_hits(query, k) -> list[dict]` with "content" and "source",
    and `publish() -> dict`, which the Agent calls once at task end (#228)."""

    async def recall(self, query: str, k: int = 5) -> list[str]: ...

    async def commit(self, content: str, kind: str = "fact", tags: str = "") -> None: ...


@runtime_checkable
class ToolProvider(Protocol):
    """tools() lists OpenAI-function-shaped specs; call() runs one.

    Optional, read with getattr and not part of the Protocol: a class attribute
    `knowledge_only = True` says the provider answers from what the agent
    already has (its bundle, its skills) and reaches nothing with a credential.
    Only such providers stay offered while the agent answers another agent's
    task (tenishai/kobo#310); the default is credentialed, so a new provider is
    withheld there until it opts in.
    """

    async def tools(self) -> list[dict]: ...

    async def call(self, name: str, payload: dict) -> str: ...


TOOL_OUTCOMES = ("ok", "empty", "error", "blocked")


class ToolResult(str):
    """A tool result's text, plus how the call ended (#268).

    A str, so every caller that treats a result as text still works and a
    provider's `call -> str` signature still holds. `outcome` is one of
    TOOL_OUTCOMES, or None when nothing on the code path said. Agent._call_tool
    sets it from the branch it took; a provider may set it first ("empty" for a
    result with no content, "blocked" for a refusal). Any str operation (a `+`)
    returns a plain str and drops it, so read it before changing the text.
    """

    outcome: str | None

    def __new__(cls, text: str, outcome: str | None = None) -> ToolResult:
        if outcome is not None and outcome not in TOOL_OUTCOMES:
            raise ValueError(f"unknown tool outcome {outcome!r}")
        obj = super().__new__(cls, text)
        obj.outcome = outcome
        return obj


class ToolCallBlocked(Exception):
    """A provider's server refused this one call by policy (#268): no grant,
    no scope, operator-only. Agent._call_tool reports it as a "blocked:" result
    with outcome "blocked", not as a "tool error:"."""


class ToolProviderRefused(RuntimeError):
    """A provider's server answered and refused this caller (daedalus#156).

    Distinct from an outage: the server is up and has said no, so retrying or
    restarting it changes nothing. MultiToolProvider drops that provider's
    tools for the turn instead of failing the whole turn.
    """


@runtime_checkable
class CheckpointStore(Protocol):
    async def save(self, task_id: str, state: dict) -> None: ...

    async def load(self, task_id: str) -> dict | None: ...

    async def drop(self, task_id: str) -> None: ...


@runtime_checkable
class Budget(Protocol):
    def spend(self, tokens: int) -> None: ...

    def remaining(self) -> int | None: ...  # None = unlimited


@runtime_checkable
class Approver(Protocol):
    async def approve(self, action: str, plan: dict) -> bool: ...


class NoMemory:
    async def recall(self, query: str, k: int = 5) -> list[str]:
        return []

    async def commit(self, content: str, kind: str = "fact", tags: str = "") -> None:
        return None


class NoTools:
    async def tools(self) -> list[dict]:
        return []

    async def call(self, name: str, payload: dict) -> str:
        raise LookupError(f"no tool provider wired; cannot call {name!r}")


class NoCheckpoints:
    async def save(self, task_id: str, state: dict) -> None:
        return None

    async def load(self, task_id: str) -> dict | None:
        return None

    async def drop(self, task_id: str) -> None:
        return None


class NoBudget:
    def spend(self, tokens: int) -> None:
        return None

    def remaining(self) -> int | None:
        return None


class AllowAll:
    """Explicit opt-out of gate approval. Never the default."""

    async def approve(self, action: str, plan: dict) -> bool:
        return True


class DenyAll:
    """The default approver: gates fail closed until a real approver is wired
    (interactive chat wires a CLI prompt; headless runs stay denied)."""

    async def approve(self, action: str, plan: dict) -> bool:
        return False
