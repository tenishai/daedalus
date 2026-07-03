"""The batteries-included agent runner — a small composed Handler, not a base class."""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path

from daedalus.bundle import Bundle
from daedalus.protocols import (
    LLM,
    CheckpointStore,
    MemoryStore,
    NoCheckpoints,
    NoMemory,
    NoTools,
    ToolProvider,
    Transport,
)
from daedalus.task import Task, TaskState


class Agent:
    """Wires a bundle's persona, model, and collaborators into a Handler.

    Deterministic agents implement Handler directly and skip this class;
    they still get transports and the bundle format.
    """

    def __init__(
        self,
        bundle: Bundle,
        *,
        llm: LLM | None = None,
        memory: MemoryStore | None = None,
        tools: ToolProvider | None = None,
        checkpoints: CheckpointStore | None = None,
    ):
        self.bundle = bundle
        if llm is None:
            from daedalus.llm import LiteLLMClient

            llm = LiteLLMClient(
                bundle.spec.model.id,
                max_tokens=bundle.spec.model.max_tokens,
                api_base=bundle.spec.model.api_base,
            )
        self.llm = llm
        self.memory = memory if memory is not None else NoMemory()
        self.tools = tools if tools is not None else NoTools()
        self.checkpoints = checkpoints if checkpoints is not None else NoCheckpoints()

    @classmethod
    def load(cls, path: str | Path, **overrides) -> Agent:
        return cls(Bundle.load(path), **overrides)

    @property
    def name(self) -> str:
        return self.bundle.spec.name

    async def handle_task(self, task: Task) -> Task:
        task.state = TaskState.WORKING
        recalled = await self.memory.recall(task.user_text())
        response = await self.llm.complete(self._messages(task, recalled))
        return task.reply(response.text)

    def _messages(self, task: Task, recalled: list[str]) -> list[dict]:
        system = self.bundle.persona
        if recalled:
            system += "\n\n## Recalled context\n" + "\n".join(f"- {item}" for item in recalled)
        messages = [{"role": "system", "content": system}]
        for message in task.history:
            role = "assistant" if message.role == "agent" else "user"
            messages.append({"role": role, "content": message.text})
        return messages

    def transports(self) -> list[Transport]:
        from daedalus.transports.cli import CliTransport

        built: list[Transport] = []
        for spec in self.bundle.spec.transports:
            if spec.kind == "cli":
                built.append(CliTransport())
            else:
                warnings.warn(
                    f"transport {spec.kind!r} is not available in this version; skipping",
                    stacklevel=2,
                )
        return built

    async def serve(self) -> None:
        transports = self.transports()
        if not transports:
            raise RuntimeError("no usable transports declared in agent.yaml")
        await asyncio.gather(*(transport.serve(self) for transport in transports))
