"""The batteries-included agent runner — a small composed Handler, not a base class."""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from daedalus.bundle import Bundle, BundleError
from daedalus.enforcement import Enforcement
from daedalus.memory.teach import (
    DELETE_KEY,
    FILENAME_KEY,
    KNOWLEDGE_MARKER,
    SYSTEM_TASK_KEY,
    TEACH_MARKER,
    TOOL_MARKER,
    apply_knowledge,
    apply_knowledge_delete,
    apply_teach,
    teach_authorized,
)
from daedalus.memory.tooling import apply_tool
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
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState


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
        self.enforcement = Enforcement.load(bundle.path / "tools", self._tool_overlay_dir())
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
        self._revision_lock = asyncio.Lock()
        # Per-candidate colleague re-ask stamps, alive for this agent's whole
        # process life: revision fires per teach, and without this one stuck
        # candidate re-asked the same colleagues on every teach. A restart
        # forgets the stamps and re-asks once -- the acceptable end of the
        # trade (see revision.COLLEAGUE_REASK_COOLDOWN_S).
        self._reask_cooldowns: dict[int, float] = {}
        self._revision_fires: set[asyncio.Task] = set()

    def _emit(self, event_type: str, **fields) -> None:
        """The runtime's trace stream: --verbose renders it in the terminal;
        the kaminos trace pane consumes the same events over SSE (/events
        on the a2a transport)."""
        event = {"type": event_type, **fields}
        self.events.publish(event)
        if self.on_event is not None:
            self.on_event(event)

    def _tool_overlay_dir(self) -> Path | None:
        """The persistent overlay directory for fast-lane-delivered tools
        (arc3d Task 5), beside the techne db on the agent's own volume --
        `spec.memory.home`, mounted at /techne in live pods. Never
        `bundle.path`, which is the image and resets to its baked-in
        contents on every restart. None when no home is configured, same
        guard `_memory_from_spec` uses for `db_path` -- independent of
        `spec.memory.personal`: the home directory is the persistent volume
        mount itself, and techne.db is just one file that happens to live
        there, so a tool write needs nothing more than `home` being set."""
        home = self.bundle.spec.memory.home
        return Path(home).expanduser() / "tools-overlay" if home else None

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
        elif spec.provider == "colleagues":
            if not spec.colleagues.url:
                raise BundleError("tools.provider 'colleagues' requires tools.colleagues.url")
            from daedalus.tools.colleagues import ColleaguesToolProvider

            providers.append(
                ColleaguesToolProvider(
                    spec.colleagues.url,
                    token_env=spec.colleagues.token_env,
                    self_name=spec.colleagues.self,
                    # colleagues.remember writes here; None (no techne memory
                    # configured) is a polite degrade, not a wiring error.
                    db_path=getattr(self.memory, "db_path", None),
                )
            )

        # The github sidecar (room PR broker, 2026-08-27 spec): composes
        # ALONGSIDE whatever primary provider the elif chain above picked --
        # deliberately not a `provider` value, so an agent keeps colleagues
        # AND gains github.* the way builtin groups already stack.
        if spec.github.broker_url:
            from daedalus.tools.github import GitHubToolProvider

            providers.append(
                GitHubToolProvider(
                    spec.github.broker_url,
                    token_env=spec.github.token_env,
                    self_name=spec.github.self,
                )
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
        # Deferred: matches this file's existing convention of importing
        # daedalus.tools.* at point of use rather than at module level (see
        # the tools_from_spec provider branches above), not a real cycle --
        # colleagues.py imports nothing from daedalus.agent.
        from daedalus.tools.colleagues import (
            DEPTH_KEY,
            consulted_colleagues,
            current_context_id,
            current_delegation_depth,
        )

        current_context_id.set(task.context_id or task.id)
        consulted_colleagues.set([])
        # cabal#144 — carry the inbound hop count so an outbound ask can send
        # depth+1. An unreadable value is treated as the TOP of the chain
        # rather than as an error: a malformed number must not be a way to
        # claim a shallower depth than you have, and anything asking onward
        # from here is still stamped 1 and bounded from there.
        #
        # OverflowError IS NOT REDUNDANT (#40 pre-pass). `int(float("inf"))`
        # raises it rather than ValueError, and an infinity gets here: Task
        # metadata has no value-type constraint, it rides through verbatim from
        # the wire, and Starlette's Request.json() is json.loads, which accepts
        # a bare `Infinity` token. Without this arm the exception escapes
        # handle_task entirely and the transport turns it into an HTTP 500 --
        # a crash, in the branch whose stated property is that garbage degrades.
        # (`nan` lands on ValueError and always worked.) cabal's own parser had
        # the identical gap; both ends are fixed.
        try:
            _inbound_depth = max(0, int(task.metadata.get(DEPTH_KEY, 0)))
        except (TypeError, ValueError, OverflowError):
            _inbound_depth = 0
        current_delegation_depth.set(_inbound_depth)
        # A BACKGROUND TASK SPAWNED BELOW THIS LINE INHERITS THE DEPTH, because
        # asyncio.create_task copies the current context (#40 r1 Nit, filed as
        # daedalus#42). _start_revision_pass does exactly that, and its own ask
        # is a FRESH chain that would be stamped parent+1 and can be refused for
        # the parent's depth. Left as-is: current_context_id inherits the same
        # way, and the two call sites reachable from HERE -- _handle_teach and
        # _handle_knowledge -- both sit behind teach_authorized, which requires
        # a verified human subject. (There is a third, in serve(), and it is the
        # safe one: it runs at startup outside any handle_task, so it inherits
        # the contextvar's default rather than a chain's depth. Naming two of
        # three was an incomplete enumeration in a comment about which sites are
        # gated -- #40 r2 Nit.) No legitimate producer sends a depth key on such
        # a task today; revisit if teach becomes reachable from an agent
        # subject.
        marker = task.metadata.get(SYSTEM_TASK_KEY)
        if marker is not None:
            return await self._dispatch_system_task(marker, task)
        if self.enforcement.locked:
            name, _ = self.enforcement.load_error
            return task.reply(
                f"My orchestrator file {name} failed to load. Fix or remove it in the room.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        pre = await self.enforcement.run_pre_task(task.user_text())
        if pre.blocked:
            return task.reply(
                f"A rule stopped this task: {pre.detail}.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        task.state = TaskState.WORKING
        recalled = await self.memory.recall(task.user_text())
        if recalled:
            self._emit("recall", count=len(recalled))
        result = await self._loop(task, self._messages(task, recalled, notices=pre.notices))
        if result.agent_text():
            post = await self.enforcement.run_post_draft(result.agent_text())
            if post.blocked:
                # The unlawful draft is discarded, never partially sent: it
                # must not linger in history where task_to_a2a would
                # serialize it to a2a callers, or a persisted Task (Slack
                # threads, a2a taskId continuation) would feed it back into
                # the model's context on the next turn.
                result.discard_last_agent_message()
                return result.reply(
                    f"A rule stopped this reply: {post.detail}.",
                    state=TaskState.FAILED,
                    reason=ReasonCode.TOOL_BLOCKED,
                )
            result.set_agent_text(post.text)
            # Attribution law, runtime-enforced (2026-08-27 ruling): when
            # this task consulted colleagues and the draft does not name
            # every one of them, the record is appended deterministically.
            # The standing colleagues.md instruction asks the model to
            # attribute; this line is what makes the law hold when it
            # doesn't. Appended AFTER post_draft enforcement so a hook's
            # rewrite can't strip it.
            consulted = consulted_colleagues.get() or []
            unnamed = [c for c in consulted if c not in result.agent_text()]
            if unnamed:
                result.set_agent_text(
                    result.agent_text()
                    + "\n\n_(consulted: " + ", ".join(consulted) + ")_"
                )
        return result

    async def _dispatch_system_task(self, marker: str, task: Task) -> Task:
        """Route a system-task-marked task to its handler, pre-model. An
        unrecognized marker is refused outright rather than falling through
        to the model, which would otherwise treat an operator-only envelope
        as an ordinary chat turn."""
        if marker == TEACH_MARKER:
            return await self._handle_teach(task)
        if marker == KNOWLEDGE_MARKER:
            return await self._handle_knowledge(task)
        if marker == TOOL_MARKER:
            return await self._handle_tool(task)
        self._emit("system_task_refused", marker=marker)
        return task.reply(
            f"Unknown system task {marker!r}. Refusing rather than guessing.",
            state=TaskState.FAILED,
            reason=ReasonCode.POLICY_VIOLATION,
        )

    async def _handle_teach(self, task: Task) -> Task:
        """The operator teach lane: write the note into techne now, no
        model, no enforcement, no rebuild. Sits above the enforcement
        locked check on purpose: memory writes are operator-gated by the
        transport-stamped sub, not law-gated -- a broken orchestrator file
        must not also brick teaching."""
        if not teach_authorized(task):
            self._emit("teach_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Teaching is operator-only. This connection is not an operator door.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return task.reply(
                "I have no personal memory configured, so I cannot keep this.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        filename = task.metadata.get(FILENAME_KEY, "")
        result = await asyncio.to_thread(
            apply_teach, task.user_text(), str(filename), Path(db_path)
        )
        if result.error:
            return task.reply(
                f"I could not keep that: {result.error}.", state=TaskState.FAILED
            )
        if result.already_known:
            return task.reply("I already know this one. Nothing new to keep.")
        self._emit("teach_stored", source=result.source, entries=len(result.entry_ids))
        fire = self._start_revision_pass()
        if fire is not None:
            self._revision_fires.add(fire)
            fire.add_done_callback(self._revision_fires.discard)
        return task.reply("Learned. I will remember that.")

    async def _handle_knowledge(self, task: Task) -> Task:
        """The operator knowledge lane: a knowledge FILE's edit or delete,
        landed now, no model, no enforcement, no rebuild. Same
        authorization gate as teach -- knowledge writes are exactly as
        operator-grade, so teach_authorized is reused verbatim rather than
        reimplemented. DELETE_KEY in metadata routes to
        apply_knowledge_delete; its absence is a write (apply_knowledge)."""
        if not teach_authorized(task):
            self._emit("knowledge_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Knowledge writes are operator-only. This connection is not an operator door.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return task.reply(
                "I have no personal memory configured, so I cannot keep this.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        filename = task.metadata.get(FILENAME_KEY, "")
        if task.metadata.get(DELETE_KEY):
            result = await asyncio.to_thread(
                apply_knowledge_delete, str(filename), Path(db_path)
            )
            if result.error:
                return task.reply(
                    f"I could not remove that: {result.error}.", state=TaskState.FAILED
                )
            self._emit(
                "knowledge_deleted", source=result.source, entries=len(result.entry_ids)
            )
            return task.reply("Forgotten. I no longer keep that.")
        result = await asyncio.to_thread(
            apply_knowledge, task.user_text(), str(filename), Path(db_path)
        )
        if result.error:
            return task.reply(
                f"I could not keep that: {result.error}.", state=TaskState.FAILED
            )
        if result.already_known:
            return task.reply("I already know this one. Nothing new to keep.")
        self._emit("knowledge_stored", source=result.source, entries=len(result.entry_ids))
        fire = self._start_revision_pass()
        if fire is not None:
            self._revision_fires.add(fire)
            fire.add_done_callback(self._revision_fires.discard)
        return task.reply("Learned. I will remember that.")

    async def _handle_tool(self, task: Task) -> Task:
        """The operator tool lane (arc3d Task 5): a fast-delivered tool
        file, re-validated by THIS runtime against the exact same
        mechanical contract kobo's room applies (the fast lane skips CI by
        definition, so kobo's own validation is never trusted alone) and,
        only once it passes, written into the overlay directory on the
        agent's persistent volume -- never the image bundle, which resets
        to its baked-in contents on every restart. Same authorization gate
        as teach/knowledge: tool writes are exactly as operator-grade.

        No hot reload: the enforcement loader (daedalus.enforcement.
        Enforcement.load) only re-runs at process boot, so a tool delivered
        here is inert in this process no matter what -- the reply says so
        plainly rather than implying anything more."""
        if not teach_authorized(task):
            self._emit("tool_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Tool writes are operator-only. This connection is not an operator door.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        overlay_dir = self._tool_overlay_dir()
        if overlay_dir is None:
            return task.reply(
                "I have no persistent tools directory configured, so I cannot keep this.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        filename = task.metadata.get(FILENAME_KEY, "")
        result = await asyncio.to_thread(
            apply_tool,
            task.user_text(),
            str(filename),
            self.bundle.path / "tools",
            overlay_dir,
        )
        if result.error:
            self._emit("tool_refused", error=result.error)
            return task.reply(
                f"I could not keep that tool: {result.error}.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        self._emit("tool_stored", filename=result.filename)
        return task.reply("Tool delivered. It will be active after my next restart.")

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
            truncated = response.finish_reason == "length"
            if truncated:
                # Silent truncation looks like a complete reply to every
                # surface downstream (finding #27, born as kobo #1/#4) —
                # trace it AND mark the reply so operators see the cut.
                self._emit("truncated", max_tokens=self.bundle.spec.model.max_tokens)
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
                text = response.text
                if truncated:
                    text += (
                        f"\n\n[reply truncated: hit the model's max_tokens "
                        f"limit ({self.bundle.spec.model.max_tokens})]"
                    )
                return task.reply(text)

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

    def _messages(
        self, task: Task, recalled: list[str], notices: list[str] | None = None
    ) -> list[dict]:
        system = self.bundle.persona
        if recalled:
            system += "\n\n## Recalled context\n" + "\n".join(f"- {item}" for item in recalled)
        if notices:
            system += "\n\n## Notices\n" + "\n".join(f"- {item}" for item in notices)
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
                card = self.bundle.card
                if self.enforcement.active:
                    # A new dict -- the on-disk card / bundle.card in memory
                    # is never mutated to carry the stamp.
                    card = {**card, "enforcement": self.enforcement.card_stamp()}
                built.append(
                    A2aTransport(
                        card=card,
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

    def _colleagues_for_revision(self):
        """The colleagues client the revision pass's colleague branch
        corroborates through (directory fetch + ask-colleague), by the same
        client/config the tool provider itself uses.

        Prefers REUSING the agent's own wired ColleaguesToolProvider --
        whether tools.provider is 'colleagues' directly, or composed behind
        MultiToolProvider alongside other providers -- since that is the
        exact client (and, when configured, the exact bearer token) the
        model's own colleagues.ask_colleague tool calls go through. Falls
        back to constructing a fresh one from the same bundle spec
        (tools.colleagues) when no such provider is reachable, e.g. the
        bundle's tools.provider is 'mcp' or 'liminal' but a colleague-sourced
        fact is still staged from an earlier configuration. Returns None
        when even that is impossible (no tools.colleagues.url configured
        anywhere) -- the colleague branch then isolates that as a
        per-candidate corroboration failure, same as any other.
        """
        from daedalus.tools.colleagues import ColleaguesToolProvider

        if isinstance(self.tools, ColleaguesToolProvider):
            return self.tools
        for provider in getattr(self.tools, "providers", []):
            if isinstance(provider, ColleaguesToolProvider):
                return provider

        spec = self.bundle.spec.tools.colleagues
        if not spec.url:
            return None
        return ColleaguesToolProvider(
            spec.url,
            token_env=spec.token_env,
            self_name=spec.self,
            db_path=getattr(self.memory, "db_path", None),
        )

    def _start_revision_pass(self) -> asyncio.Task | None:
        """Schedule the post-boot belief-revision pass once, in the background.

        Absent memory.revision (the default) this returns immediately without
        even importing daedalus.memory.revision -- a bundle that hasn't opted
        in gets byte-identical behavior to a runtime that never heard of it.
        An unknown policy value was already normalized to None (and warned
        about) by MemorySpec itself, so the only value reaching here is a
        known one ("operator" today).
        """
        policy = self.bundle.spec.memory.revision
        if policy is None:
            return None
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            warnings.warn(
                "memory.revision is set but memory.personal is not 'techne' "
                "(no techne db to revise); disabling",
                stacklevel=2,
            )
            return None

        from daedalus.memory.revision import run_revision_pass

        colleagues = self._colleagues_for_revision()

        async def _pass() -> None:
            async with self._revision_lock:
                await run_revision_pass(
                    db_path,
                    self.llm,
                    policy=policy,
                    emit=self._emit,
                    colleagues=colleagues,
                    reask_cooldowns=self._reask_cooldowns,
                )

        return asyncio.create_task(_pass())

    async def serve(self) -> None:
        transports = self.transports()
        if not transports:
            raise RuntimeError("no usable transports declared in agent.yaml")
        revision_task = self._start_revision_pass()
        try:
            await asyncio.gather(*(transport.serve(self) for transport in transports))
        finally:
            if revision_task is not None:
                revision_task.cancel()
            await self.aclose()
