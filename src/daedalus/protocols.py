"""Protocol seams and their no-op defaults.

Every cross-cutting concern is a Protocol with a no-op default injected at
wiring time — never None-checked in the hot path. Real backends arrive via
extras and entry points (techne, limenal, slack).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from daedalus.task import Task


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLM(Protocol):
    """Model, max_tokens, etc. are construction-time config, not call args."""

    async def complete(self, messages: list[dict]) -> LLMResponse: ...


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
    async def recall(self, query: str, k: int = 5) -> list[str]: ...

    async def commit(self, content: str, kind: str = "fact") -> None: ...


@runtime_checkable
class ToolProvider(Protocol):
    async def tools(self) -> list[dict]: ...

    async def call(self, name: str, payload: dict) -> str: ...


@runtime_checkable
class CheckpointStore(Protocol):
    async def save(self, task_id: str, state: dict) -> None: ...

    async def load(self, task_id: str) -> dict | None: ...

    async def drop(self, task_id: str) -> None: ...


@runtime_checkable
class Budget(Protocol):
    def reserve(self, tokens: int) -> None: ...

    def refund(self, tokens: int) -> None: ...


@runtime_checkable
class Approver(Protocol):
    async def approve(self, action: str, plan: dict) -> bool: ...


class NoMemory:
    async def recall(self, query: str, k: int = 5) -> list[str]:
        return []

    async def commit(self, content: str, kind: str = "fact") -> None:
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
    def reserve(self, tokens: int) -> None:
        return None

    def refund(self, tokens: int) -> None:
        return None


class AllowAll:
    """Placeholder approver until gates are enforced in v0.2."""

    async def approve(self, action: str, plan: dict) -> bool:
        return True
