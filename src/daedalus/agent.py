"""The batteries-included agent runner — a small composed Handler, not a base class."""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Callable
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


_SALVAGE_CAP = 4


def _json_objects(text: str):
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            yield obj
        index = start + max(consumed, 1)


def salvage_tool_calls(text: str, known: set[str]) -> list[ToolCall]:
    """Recover tool calls that local models emit as text instead of structure.

    (Qwen-Coder on vLLM writes {"name": ..., "arguments": ...} inside <tools>
    tags — mimicking the definition format — so the server parser misses it.)

    Known names get executed. Unknown-but-tool-shaped names (containing '__'
    or '.') are salvaged too so the loop answers with 'unknown tool' feedback
    the model can correct from, instead of dumping the blob on the user —
    execution is still hard-gated on the offered-tool map. Plain prose,
    malformed JSON, and non-toolish names pass through as text. Duplicates
    are collapsed (degenerating models repeat themselves) and at most
    _SALVAGE_CAP calls are taken per reply.
    """
    calls: list[ToolCall] = []
    seen: set[tuple] = set()
    for obj in _json_objects(text):
        name, args = obj.get("name"), obj.get("arguments")
        if not isinstance(name, str) or not isinstance(args, dict):
            continue
        if name not in known and "__" not in name and "." not in name:
            continue
        key = (name, json.dumps(args, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(id=f"salvaged-{len(calls)}", name=name, arguments=args))
        if len(calls) == _SALVAGE_CAP:
            return calls
    return calls


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
        on_event: Callable[[dict], None] | None = None,
    ):
        self.bundle = bundle
        if llm is None:
            import os

            from daedalus.llm import LiteLLMClient

            model_spec = bundle.spec.model
            api_key = os.environ.get(model_spec.api_key_env) if model_spec.api_key_env else None
            llm = LiteLLMClient(
                model_spec.id,
                max_tokens=model_spec.max_tokens,
                api_base=model_spec.api_base,
                api_key=api_key,
            )
        self.llm = llm
        self.memory = memory if memory is not None else self._memory_from_spec()
        self.tools = tools if tools is not None else self._tools_from_spec()
        self.checkpoints = (
            checkpoints if checkpoints is not None else self._checkpoints_from_spec()
        )
        self.approver = approver if approver is not None else DenyAll()
        self.on_event = on_event
        from daedalus.events import EventBus

        self.events = EventBus()

    def _emit(self, event_type: str, **fields) -> None:
        """The runtime's trace stream: --verbose renders it in the terminal;
        the kaminos trace pane consumes the same events over SSE (/events
        on the a2a transport)."""
        event = {"type": event_type, **fields}
        self.events.publish(event)
        if self.on_event is not None:
            self.on_event(event)

    def _memory_from_spec(self) -> MemoryStore:
        spec = self.bundle.spec.memory
        if spec.personal == "techne":
            try:
                import techne  # noqa: F401  — fail at load time, not mid-conversation
            except ImportError as exc:
                raise BundleError(
                    "memory.personal is 'techne' but techne is not installed — "
                    "pip install -e path/to/techne (github.com/dimartin/techne)"
                ) from exc
            from daedalus.memory.techne import TechneMemoryStore

            db_path = Path(spec.home).expanduser() / "techne.db" if spec.home else None
            return TechneMemoryStore(db_path=db_path)
        return NoMemory()

    def _checkpoints_from_spec(self) -> CheckpointStore:
        if self.bundle.spec.checkpoints == "sqlite":
            from daedalus.checkpoints import SqliteCheckpoints

            path = Path.home() / ".cache" / "daedalus" / self.bundle.spec.name
            return SqliteCheckpoints(path / "checkpoints.db")
        return NoCheckpoints()

    def _budget(self):
        limit = self.bundle.spec.model.budget.per_task_tokens
        if limit:
            from daedalus.budget import TokenBudget

            return TokenBudget(limit)
        from daedalus.protocols import NoBudget

        return NoBudget()

    def _tools_from_spec(self) -> ToolProvider:
        spec = self.bundle.spec.tools
        providers: list[ToolProvider] = []

        for key, config in spec.builtin.items():
            if key != "files":
                raise BundleError(f"unknown builtin tool group {key!r} (available: files)")
            from daedalus.tools.builtin import FilesToolProvider

            try:
                providers.append(FilesToolProvider(**config))
            except (TypeError, ValueError) as exc:
                raise BundleError(f"tools.builtin.files: {exc}") from exc

        if spec.provider == "mcp":
            try:
                import mcp  # noqa: F401  — fail at load time, not mid-conversation
            except ImportError as exc:
                raise BundleError(
                    "tools.provider is 'mcp' but the mcp extra is not installed — "
                    "pip install 'daedalus[mcp]'"
                ) from exc
            from daedalus.tools.mcp import MCPToolProvider

            providers.append(MCPToolProvider(spec.servers))
        elif spec.provider == "liminal":
            try:
                import liminal  # noqa: F401  — fail at load time, not mid-conversation
            except ImportError as exc:
                raise BundleError(
                    "tools.provider is 'liminal' but liminal is not installed — "
                    "pip install -e path/to/liminal (github.com/tenishai/liminal)"
                ) from exc
            if not spec.liminal.url:
                raise BundleError("tools.provider 'liminal' requires tools.liminal.url")
            from daedalus.tools.liminal import LiminalToolProvider

            providers.append(
                LiminalToolProvider(spec.liminal.url, token_env=spec.liminal.token_env)
            )

        if not providers:
            return NoTools()
        if len(providers) == 1:
            return providers[0]
        from daedalus.tools.compose import MultiToolProvider

        return MultiToolProvider(providers)

    @classmethod
    def load(cls, path: str | Path, **overrides) -> Agent:
        return cls(Bundle.load(path), **overrides)

    @property
    def name(self) -> str:
        return self.bundle.spec.name

    async def handle_task(self, task: Task) -> Task:
        task.state = TaskState.WORKING
        recalled = await self.memory.recall(task.user_text())
        if recalled:
            self._emit("recall", count=len(recalled))
        return await self._loop(task, self._messages(task, recalled))

    async def resume(self, task_id: str) -> Task | None:
        """Continue a checkpointed task (budget-exhausted or crashed).
        Returns None if no checkpoint exists. The budget starts fresh."""
        state = await self.checkpoints.load(task_id)
        if state is None:
            return None
        task = Task.model_validate(state["task"])
        task.state = TaskState.WORKING
        return await self._loop(task, state["messages"])

    async def _loop(self, task: Task, messages: list[dict]) -> Task:
        try:
            allowed = await self._allowed_tools()
        except Exception as exc:
            # A down gateway is a task outcome, not a transport 500.
            self._emit("tool_provider_error", error=str(exc))
            return task.reply(
                f"tool provider unavailable: {exc}",
                state=TaskState.FAILED,
                reason=ReasonCode.ESCALATION,
            )
        self._emit("tools_offered", names=[spec["name"] for spec in allowed])
        llm_tools = [self._to_openai(spec) for spec in allowed] or None
        by_munged = {_munge(spec["name"]): spec["name"] for spec in allowed}
        budget = self._budget()

        last_attempt = ""
        for _ in range(self.bundle.spec.tools.max_steps):
            await self.checkpoints.save(
                task.id, {"task": task.model_dump(), "messages": messages}
            )
            if budget.remaining() == 0:
                # checkpoint intentionally kept: re-fund and resume()
                return task.reply(
                    f"stopped: token budget exhausted "
                    f"({self.bundle.spec.model.budget.per_task_tokens})",
                    state=TaskState.FAILED,
                    reason=ReasonCode.BUDGET_EXHAUSTED,
                )
            response = await self.llm.complete(messages, tools=llm_tools)
            budget.spend(response.input_tokens + response.output_tokens)
            if budget.remaining() is not None:
                self._emit(
                    "budget",
                    spent=response.input_tokens + response.output_tokens,
                    remaining=budget.remaining(),
                )
            if response.reasoning:
                self._emit("reasoning", text=response.reasoning)
            tool_calls = response.tool_calls
            if not tool_calls and llm_tools:
                tool_calls = salvage_tool_calls(response.text, set(by_munged))
                if tool_calls:
                    self._emit("salvage", count=len(tool_calls))
            if not tool_calls:
                await self.checkpoints.drop(task.id)
                return task.reply(response.text)

            messages.append(self._assistant_message(response.text, tool_calls))
            for call in tool_calls:
                canonical = by_munged.get(call.name, call.name)  # trace speaks canonical
                self._emit("tool_call", name=canonical, arguments=call.arguments)
                result = await self._execute(call, by_munged)
                self._emit("tool_result", name=canonical, content=result)
                last_attempt = f"{canonical} -> {result[:200]}"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        detail = f"; last attempt: {last_attempt}" if last_attempt else ""
        return task.reply(
            f"stopped: exceeded tools.max_steps "
            f"({self.bundle.spec.tools.max_steps}){detail}",
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
    def _assistant_message(text: str, tool_calls: list[ToolCall]) -> dict:
        return {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in tool_calls
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

    @staticmethod
    def _a2a_verifier(auth: dict | None):
        """The a2a transport's optional `auth` block -> JwtVerifier, or None.

        Incomplete config fails at load time, like every other extras seam."""
        if auth is None:
            return None
        if not isinstance(auth, dict):
            raise BundleError("a2a transport 'auth' must be a mapping")
        missing = [key for key in ("jwks_url", "issuer", "audience") if not auth.get(key)]
        if missing:
            raise BundleError(
                f"a2a transport auth requires {', '.join(missing)} "
                "(jwks_url, issuer, audience)"
            )
        try:
            import joserfc  # noqa: F401  — fail at load time, not on first request
        except ImportError as exc:
            raise BundleError(
                "a2a transport auth requires joserfc — pip install 'daedalus[a2a]'"
            ) from exc
        from daedalus.transports.a2a_auth import JwtVerifier

        return JwtVerifier(
            jwks_url=str(auth["jwks_url"]),
            issuer=str(auth["issuer"]),
            audience=str(auth["audience"]),
        )

    def transports(self) -> list[Transport]:
        from daedalus.transports.cli import CliTransport

        built: list[Transport] = []
        for spec in self.bundle.spec.transports:
            if spec.kind == "cli":
                built.append(CliTransport())
            elif spec.kind == "a2a":
                try:
                    import fastapi  # noqa: F401
                    import uvicorn  # noqa: F401
                except ImportError as exc:
                    raise BundleError(
                        "transport 'a2a' requires the a2a extra — "
                        "pip install 'daedalus[a2a]'"
                    ) from exc
                from daedalus.transports.a2a import A2aTransport

                options = spec.options()
                built.append(
                    A2aTransport(
                        card=self.bundle.card,
                        host=str(options.get("host", "127.0.0.1")),
                        port=int(options.get("port", 9200)),
                        events=self.events,
                        verifier=self._a2a_verifier(options.get("auth")),
                    )
                )
            elif spec.kind == "slack":
                try:
                    import slack_sdk  # noqa: F401
                except ImportError as exc:
                    raise BundleError(
                        "transport 'slack' requires the slack extra — "
                        "pip install 'daedalus[slack]'"
                    ) from exc
                from daedalus.transports.slack import SlackTransport

                options = spec.options()
                channel = options.get("channel")
                if not channel:
                    raise BundleError("slack transport requires a 'channel' (channel ID)")
                built.append(
                    SlackTransport(
                        str(channel),
                        escalation_route=self.bundle.spec.approvals.escalation.route,
                    )
                )
            else:
                warnings.warn(
                    f"transport {spec.kind!r} is not available in this version; skipping",
                    stacklevel=2,
                )
        return built

    async def aclose(self) -> None:
        """Close closable collaborators from the task that used them —
        MCP stdio sessions object loudly to cross-task teardown."""
        closer = getattr(self.tools, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # teardown must never mask the real work
                pass

    async def serve(self) -> None:
        transports = self.transports()
        if not transports:
            raise RuntimeError("no usable transports declared in agent.yaml")
        try:
            await asyncio.gather(*(transport.serve(self) for transport in transports))
        finally:
            await self.aclose()
