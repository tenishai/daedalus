"""The batteries-included agent runner — a small composed Handler, not a base class."""

from __future__ import annotations

import asyncio
import json
import warnings
from fnmatch import fnmatch
from pathlib import Path

from daedalus.bundle import Bundle, BundleError
from daedalus.protocols import (
    LLM,
    Approver,
    CheckpointStore,
    DenyAll,
    MemoryStore,
    NoCheckpoints,
    NoMemory,
    NoTools,
    ToolCall,
    ToolProvider,
    Transport,
)
from daedalus.task import ReasonCode, Task, TaskState


def _munge(name: str) -> str:
    """Canonical `server.tool` -> LLM-safe function name (models dislike dots)."""
    return name.replace(".", "__")


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
        approver: Approver | None = None,
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
        self.tools = tools if tools is not None else self._tools_from_spec()
        self.checkpoints = checkpoints if checkpoints is not None else NoCheckpoints()
        self.approver = approver if approver is not None else DenyAll()

    def _tools_from_spec(self) -> ToolProvider:
        spec = self.bundle.spec.tools
        if spec.provider == "mcp":
            try:
                import mcp  # noqa: F401  — fail at load time, not mid-conversation
            except ImportError as exc:
                raise BundleError(
                    "tools.provider is 'mcp' but the mcp extra is not installed — "
                    "pip install 'daedalus[mcp]'"
                ) from exc
            from daedalus.tools.mcp import MCPToolProvider

            return MCPToolProvider(spec.servers)
        if spec.provider == "limenal":
            warnings.warn("tools.provider=limenal arrives in v0.3; using none", stacklevel=2)
        return NoTools()

    @classmethod
    def load(cls, path: str | Path, **overrides) -> Agent:
        return cls(Bundle.load(path), **overrides)

    @property
    def name(self) -> str:
        return self.bundle.spec.name

    async def handle_task(self, task: Task) -> Task:
        task.state = TaskState.WORKING
        recalled = await self.memory.recall(task.user_text())
        messages = self._messages(task, recalled)

        allowed = await self._allowed_tools()
        llm_tools = [self._to_openai(spec) for spec in allowed] or None
        by_munged = {_munge(spec["name"]): spec["name"] for spec in allowed}

        for _ in range(self.bundle.spec.tools.max_steps):
            response = await self.llm.complete(messages, tools=llm_tools)
            if not response.tool_calls:
                return task.reply(response.text)

            messages.append(self._assistant_message(response))
            for call in response.tool_calls:
                result = await self._execute(call, by_munged)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        return task.reply(
            f"stopped: exceeded tools.max_steps ({self.bundle.spec.tools.max_steps})",
            state=TaskState.FAILED,
            reason=ReasonCode.POLICY_VIOLATION,
        )

    async def _allowed_tools(self) -> list[dict]:
        """Tier-1 ceiling, enforced at listing: the model never sees a tool it
        can't call. Empty allowlist = deny everything (fail closed)."""
        allow = self.bundle.spec.tools.allow
        if not allow:
            return []
        return [
            spec
            for spec in await self.tools.tools()
            if any(fnmatch(spec["name"], pattern) for pattern in allow)
        ]

    async def _execute(self, call: ToolCall, by_munged: dict[str, str]) -> str:
        """Tier-1 re-checked at call time (a hallucinated name misses the map),
        then tier-2 gates, then the provider."""
        name = by_munged.get(call.name)
        if name is None:
            return f"error: unknown or disallowed tool {call.name!r}"

        gate = next(
            (g for g in self.bundle.spec.approvals.gates if fnmatch(name, g.match)), None
        )
        if gate is not None:
            plan = {"tool": name, "arguments": call.arguments, "require": gate.require}
            if not await self.approver.approve(name, plan):
                return f"blocked: {name} requires approval ({gate.require}) and it was denied"

        try:
            return await self.tools.call(name, call.arguments)
        except Exception as exc:
            return f"tool error: {exc}"

    @staticmethod
    def _to_openai(spec: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": _munge(spec["name"]),
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
            },
        }

    @staticmethod
    def _assistant_message(response) -> dict:
        return {
            "role": "assistant",
            "content": response.text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in response.tool_calls
            ],
        }

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
