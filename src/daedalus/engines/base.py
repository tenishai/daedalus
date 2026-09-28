"""The engine contract: what an engine is given for one turn, and what it returns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from daedalus.bundle import AgentSpec
    from daedalus.protocols import Budget, CheckpointStore
    from daedalus.task import Task


@dataclass
class EngineTurn:
    """Everything an engine may use for one turn, and nothing else.

    The Agent builds this; the engine never reaches back into the Agent. `call_tool`
    takes a canonical tool name (`files.read`) and runs the Agent's allowlist check,
    approval gates and tool provider, so every engine gets the same enforcement.
    """

    task: Task
    messages: list[dict]  # system prompt, history and the user's message, as built today
    tools: list[dict]  # the allowlisted tool specs, canonical names
    call_tool: Callable[[str, dict], Awaitable[str]]
    emit: Callable[..., None]  # the trace stream (Agent._emit)
    budget: Budget
    checkpoints: CheckpointStore
    spec: AgentSpec  # limits (tools.*) and model settings, read-only
    # For tools the ENGINE runs itself (Claude Code's built-ins): the bundle's gates and
    # approver. Returns None to allow, or the refusal to show the model. None here means
    # nobody can approve, and an engine must deny.
    approve: Callable[[str, dict], Awaitable[str | None]] | None = None


class Engine(Protocol):
    """Runs one turn of a task and returns the task with its reply and state set."""

    name: str

    async def run(self, turn: EngineTurn) -> Task: ...
