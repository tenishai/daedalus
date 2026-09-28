"""The batteries-included agent runner — a small composed Handler, not a base class."""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from daedalus.bundle import Bundle, BundleError, unmet_requirements
from daedalus.enforcement import Enforcement
from daedalus.engines.base import Engine, EngineTurn
from daedalus.engines.litellm import (
    _SALVAGE_CAP,  # noqa: F401  re-exported: tests import it from here
    LiteLLMEngine,
    salvage_tool_calls,  # noqa: F401  re-exported: tests import it from here
)
from daedalus.memory.learnings import LEARNINGS_MARKER
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
    ToolProvider,
    Transport,
)
from daedalus.task import VERIFIED_SUB_KEY, ReasonCode, Task, TaskState

log = logging.getLogger(__name__)


_WITHHELD_HEADING = "## Tools you can't use in this run"


def _with_withheld_note(messages: list[dict], withheld: set[str]) -> list[dict]:
    """Tell the model which tools were withheld and why, once (a resumed task's
    checkpointed system prompt already carries the note)."""
    system = messages[0]
    if system.get("role") != "system" or _WITHHELD_HEADING in str(system.get("content")):
        return messages
    note = (
        f"\n\n{_WITHHELD_HEADING}\n"
        "These tools need approval, and nobody can give it in this run, so they are "
        f"not available: {', '.join(sorted(withheld))}. Don't try to call them. If the "
        "task needs one, say what you would have done with it."
    )
    return [{**system, "content": system["content"] + note}, *messages[1:]]


#: Engines an operator can choose with --engine / DAEDALUS_ENGINE (tenishai/agents#51).
ENGINES = ("litellm", "claude-code")


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
        engine: str | None = None,
    ):
        self.bundle = bundle
        # Which engine runs the bundle is the operator's choice (tenishai/agents#51):
        # the argument, else DAEDALUS_ENGINE, else litellm. Checked here so a typo fails
        # at load, not on the first task.
        import os

        self.engine_name = engine or os.environ.get("DAEDALUS_ENGINE") or "litellm"
        if self.engine_name not in ENGINES:
            raise BundleError(
                f"unknown engine {self.engine_name!r}; choose one of {', '.join(ENGINES)}"
            )
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
        # Last, so it reports against fully-wired state. An assigned skill that
        # cannot be reached is invisible otherwise: the agent boots green and
        # simply never uses it.
        self._warn_on_unusable_skills()

    def _operator_gate(self, task: Task, lane: str) -> bool:
        """The shared gate on the three operator-only lanes, and the ONE place
        that records which sub reached them.

        EMITS ON BOTH OUTCOMES, and the allow event is the point (#47). The
        refusal events already existed; refusals alone cannot tell you what
        your LEGITIMATE callers present, and that is exactly the question
        blocking the inversion of this gate from a blocklist to an
        `operator:` allowlist. kobo's sub is a property of one env var in one
        cluster (`CABAL_OPERATOR_TOKEN`, propagated through cabal's re-mint),
        so no amount of reading either repo settles it -- it has to be read
        off a running agent. `<lane>_allowed` is how.

        The refusal event names are unchanged on purpose: the kaminos trace
        pane consumes this stream, and renaming a signal to add a sibling is
        how you break a consumer for no reason."""
        sub = task.metadata.get(VERIFIED_SUB_KEY)
        allowed = teach_authorized(task)
        self._emit(f"{lane}_allowed" if allowed else f"{lane}_refused", sub=sub)
        return allowed

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
                    "pip install -e path/to/techne (github.com/tenishai/techne)"
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

        if self.bundle.skills:
            # First in the list, so `skills.load` wins any name collision with a
            # provider that happens to offer the same name -- an agent's own
            # assigned skills are not something a remote server gets to shadow.
            from daedalus.tools.skills import SkillsToolProvider

            providers.append(SkillsToolProvider(self.bundle.skills))

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

        # The cloudtrail sidecar (cloud-access integration path, 2026-09-03):
        # same stacking rule as github -- a sidecar block, not a provider
        # value. Identity is the pod's own; there is no token to wire.
        if spec.cloudtrail.role_arn and spec.cloudtrail.event_data_store:
            from daedalus.tools.cloudtrail import CloudTrailToolProvider

            providers.append(
                CloudTrailToolProvider(
                    spec.cloudtrail.role_arn,
                    spec.cloudtrail.event_data_store,
                    region=spec.cloudtrail.region,
                    group_prefix=spec.cloudtrail.group_prefix,
                )
            )

        # The slack-read sidecar (2026-09-04): same stacking rule. Reach is
        # the bundle's channel allowlist; auth is the pod's own bot token.
        # `is not None`, not truthiness: an explicit empty list turns the lane
        # ON with no allowlist (every channel the app was invited to), which
        # is a different answer from omitting the block (2026-09-23).
        if spec.slack_read.channels is not None:
            from daedalus.tools.slack_read import SlackReadToolProvider

            if not spec.slack_read.channels:
                # Bundles that wrote `channels: []` got the lane OFF before
                # 2026-09-23 and get it ON now. Say so at boot, the same way
                # the transport's listening_line() does for its own reach.
                print(
                    "(slack read lane open: every channel the app is invited to)",
                    flush=True,
                )

            providers.append(
                SlackReadToolProvider(
                    spec.slack_read.channels,
                    token_env=spec.slack_read.token_env,
                    scan_limit=spec.slack_read.scan_limit,
                )
            )

        # The jira sidecar (room broker's issue-tracker read lane,
        # 2026-09-04): the github custody model applied to reads.
        if spec.jira.broker_url:
            from daedalus.tools.jira import JiraToolProvider

            providers.append(
                JiraToolProvider(
                    spec.jira.broker_url,
                    token_env=spec.jira.token_env,
                    self_name=spec.jira.self,
                )
            )

        # The liminal sidecar (external-MCP arc): gateway tools stack
        # beside the primary provider once the bundle names the gateway --
        # the same composition rule as github. Skipped when liminal IS the
        # primary above (never two clients to one gateway).
        if spec.provider != "liminal" and spec.liminal.url:
            try:
                import liminal  # noqa: F401  — fail at load time, not mid-conversation
            except ImportError as exc:
                raise BundleError(
                    "tools.liminal.url is set but liminal is not installed — "
                    "pip install -e path/to/liminal"
                ) from exc
            from daedalus.tools.liminal import LiminalToolProvider

            providers.append(
                LiminalToolProvider(
                    spec.liminal.url,
                    token_env=spec.liminal.token_env,
                    exchange_url=spec.liminal.exchange_url,
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
        if result.state is TaskState.COMPLETED:
            try:
                error = await asyncio.to_thread(self._journal_conversation, result)
            except Exception as exc:  # belt and braces: journaling never fails the task
                error = str(exc)
            if error:
                self._emit("journal_error", error=error)
        return result

    def _journal_conversation(self, task: Task) -> str | None:
        """Append the completed conversation to the journal the learnings
        summarizer reads (daily-learnings spec). Best-effort by contract:
        the task already succeeded, and a journal failure must never turn
        that success into a failure. System tasks never reach here --
        handle_task dispatches them before the model path.

        Runs under asyncio.to_thread -- i.e. on a worker thread, not the
        event loop -- so it must never call self._emit itself:
        EventBus.publish does asyncio.Queue.put_nowait, which is not
        thread-safe and can raise under asyncio debug mode. The error is
        returned as a string instead, for the (event-loop-side) call site
        in handle_task to emit."""
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return None
        from daedalus.memory import journal

        try:
            entries = []
            for message in task.history:
                # The journal is the learnings summarizer's TEXT source: base64
                # image bytes would balloon storage (megabytes per row) and mean
                # nothing to the summarizer. Strip them; a marker keeps the fact
                # that images were shared visible to the day's summary.
                # exclude_none: a message with no sender journals exactly
                # the {"role", "text"} shape it always did.
                entry = message.model_dump(exclude={"images"}, exclude_none=True)
                if message.images:
                    entry["text"] = (
                        f"{entry['text']} [shared {len(message.images)} image(s)]".strip()
                    )
                entries.append(entry)
            journal.record(
                journal.journal_path(Path(db_path)),
                task.context_id or task.id,
                entries,
            )
        except Exception as exc:
            return str(exc)
        return None

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
        if marker == LEARNINGS_MARKER:
            return await self._handle_learnings(task)
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
        if not self._operator_gate(task, "teach"):
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

    async def _handle_learnings(self, task: Task) -> Task:
        """Summarize the journal's last day into candidate memory snippets
        (daily-learnings spec). Proposes only -- nothing is stored here;
        the room's owner-approval flow rides the teach lane for that.
        Replies JSON ({"candidates": [...]}) in the task's reply text,
        which task_to_a2a carries verbatim for the room to parse."""
        from daedalus.memory import journal, learnings

        if not teach_authorized(task):
            self._emit("learnings_refused", sub=task.metadata.get(VERIFIED_SUB_KEY))
            return task.reply(
                "Only the room may ask me to summarize my day.",
                state=TaskState.FAILED,
                reason=ReasonCode.POLICY_VIOLATION,
            )
        empty = json.dumps({"candidates": []})
        db_path = getattr(self.memory, "db_path", None)
        if db_path is None:
            return task.reply(empty)
        try:
            conversations = await asyncio.to_thread(
                journal.recent, journal.journal_path(Path(db_path))
            )
        except Exception as exc:
            self._emit("learnings_error", error=str(exc))
            return task.reply(
                "My journal is unreadable today.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        if not conversations:
            return task.reply(empty)
        on_date = learnings.safe_date(task.metadata.get("date"), fallback="today")
        already = (task.metadata or {}).get("known")
        # "What you already know": probe techne with the day's own text so
        # the model can skip re-proposing established knowledge.
        probe = " ".join(
            message.get("text", "")
            for conv in conversations
            for message in conv.get("history", [])
        )[:500]
        known = await self.memory.recall(probe, k=10)
        messages = learnings.build_messages(
            self.bundle.persona,
            conversations,
            known,
            on_date=on_date,
            already=already if isinstance(already, list) else None,
        )
        try:
            response = await self.llm.complete(messages)
        except Exception as exc:
            self._emit("learnings_error", error=str(exc))
            return task.reply(
                "My summarizer failed today.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )
        candidates = learnings.parse_candidates(
            response.text or "", turns=learnings.turns_by_speaker(messages[-1]["content"])
        )
        self._emit("learnings_proposed", count=len(candidates))
        return task.reply(json.dumps({"candidates": candidates}))

    async def _handle_knowledge(self, task: Task) -> Task:
        """The operator knowledge lane: a knowledge FILE's edit or delete,
        landed now, no model, no enforcement, no rebuild. Same
        authorization gate as teach -- knowledge writes are exactly as
        operator-grade, so teach_authorized is reused verbatim rather than
        reimplemented. DELETE_KEY in metadata routes to
        apply_knowledge_delete; its absence is a write (apply_knowledge)."""
        if not self._operator_gate(task, "knowledge"):
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
        if not self._operator_gate(task, "tool"):
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
        withheld = self._unapprovable(allowed)
        if withheld:
            # Nobody can approve a gated tool in this run, so don't offer it: a model
            # offered a tool and refused every time retries a refusal it can't
            # understand. Tell it why instead (architect pattern 3f170e7aff8d918f).
            allowed = [spec for spec in allowed if spec["name"] not in withheld]
            self._emit("tools_withheld", names=sorted(withheld))
            messages = _with_withheld_note(messages, withheld)
        self._emit("tools_offered", names=[spec["name"] for spec in allowed])
        allowed_names = {spec["name"] for spec in allowed}

        async def call_tool(name: str, arguments: dict) -> str:
            return await self._call_tool(name, arguments, allowed_names)

        return await self._engine().run(
            EngineTurn(
                task=task,
                messages=messages,
                tools=allowed,
                call_tool=call_tool,
                emit=self._emit,
                budget=self._budget(),
                checkpoints=self.checkpoints,
                spec=self.bundle.spec,
                approve=self._approve_native,
                can_approve=not isinstance(self.approver, DenyAll),
            )
        )

    def _engine(self) -> Engine:
        """Built per turn, so a test that swaps `agent.llm` is honoured."""
        if self.engine_name == "claude-code":
            from daedalus.engines.claude_code import ClaudeCodeEngine

            return ClaudeCodeEngine(self.bundle.spec.engine.claude_code)
        return LiteLLMEngine(self.llm)

    def _unapprovable(self, tools: list[dict]) -> set[str]:
        """Gated tools nobody can approve in this run: the approver is still the
        fail-closed DenyAll, whose approve() always returns False. Whatever wires a
        real approver does it before any task (for example CLI chat at construction,
        or transports/slack.py _wire_approver); any run it doesn't reach keeps
        DenyAll, and so gets its gated tools withheld."""
        if not isinstance(self.approver, DenyAll):
            return set()
        gates = self.bundle.spec.approvals.gates
        return {
            spec["name"]
            for spec in tools
            if any(fnmatch(spec["name"], g.match) for g in gates)
        }

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

    async def _approve_native(self, name: str, arguments: dict) -> str | None:
        """Tier-2 gates for a tool an engine runs itself (not one of ours): None to
        allow, or the refusal. Tier 1 for those is the engine's own tool list."""
        gate = next(
            (g for g in self.bundle.spec.approvals.gates if fnmatch(name, g.match)), None
        )
        if gate is None:
            return None
        plan = {"tool": name, "arguments": arguments, "require": gate.require}
        if await self.approver.approve(name, plan):
            return None
        return f"blocked: {name} requires approval ({gate.require}) and it was denied"

    async def _call_tool(self, name: str, arguments: dict, allowed: set[str]) -> str:
        """Every engine's tool calls come through here: tier-1 re-checked at call
        time, then tier-2 gates, then the provider."""
        if name not in allowed:
            return f"error: unknown or disallowed tool {name!r}"

        gate = next(
            (g for g in self.bundle.spec.approvals.gates if fnmatch(name, g.match)), None
        )
        if gate is not None:
            plan = {"tool": name, "arguments": arguments, "require": gate.require}
            if not await self.approver.approve(name, plan):
                return f"blocked: {name} requires approval ({gate.require}) and it was denied"

        try:
            return await self.tools.call(name, arguments)
        except Exception as exc:
            return f"tool error: {exc}"

    def _skills_index(self) -> str:
        """One line per assigned skill: the id and when to use it.

        Names only. The bodies are fetched through `skills.load` -- see
        daedalus/tools/skills.py for why they cannot live here.

        Returns "" when `skills.load` is not in the allowlist, because an index
        the agent cannot act on is worse than no index: it would name fourteen
        capabilities and leave every one of them unreachable, which is the
        failure mode this whole feature exists to remove.
        """
        if not self.bundle.skills:
            return ""
        allow = self.bundle.spec.tools.allow
        if not any(fnmatch("skills.load", pattern) for pattern in allow):
            return ""
        lines = [f"- `{skill.id}` — {skill.summary}" for skill in self.bundle.skills.values()]
        return (
            "\n\n## Skills\n"
            "Assigned skills. Call `skills.load(name)` to read one in full "
            "before acting on it.\n" + "\n".join(lines)
        )

    def _warn_on_unusable_skills(self) -> None:
        """Say so, at load time, when an assigned skill cannot work.

        Two distinct silent failures, both of which look exactly like success:
        a skill whose `requires:` names tools the allowlist does not admit, and
        a bundle with skills whose allowlist omits `skills.load` entirely (so
        the index is suppressed and nothing can be fetched by name).
        """
        if not self.bundle.skills:
            return
        allow = self.bundle.spec.tools.allow
        if not any(fnmatch("skills.load", pattern) for pattern in allow):
            log.warning(
                "%d skill(s) assigned but tools.allow does not admit 'skills.load' — "
                "none of them can be loaded by name: %s",
                len(self.bundle.skills),
                ", ".join(sorted(self.bundle.skills)),
            )
        for skill_id, missing in unmet_requirements(self.bundle.skills, allow).items():
            log.warning(
                "skill %r requires %s, which tools.allow does not admit",
                skill_id,
                ", ".join(missing),
            )

    def _messages(
        self, task: Task, recalled: list[str], notices: list[str] | None = None
    ) -> list[dict]:
        system = self.bundle.persona
        index = self._skills_index()
        if index:
            system += index
        if recalled:
            system += "\n\n## Recalled context\n" + "\n".join(f"- {item}" for item in recalled)
        if notices:
            system += "\n\n## Notices\n" + "\n".join(f"- {item}" for item in notices)
        messages = [{"role": "system", "content": system}]
        for message in task.history:
            role = "assistant" if message.role == "agent" else "user"
            if message.images:
                # Vision (wave 3): OpenAI-style content parts -- litellm
                # translates per provider. Text part first, then every
                # image as a base64 data URI.
                parts: list[dict] = [{"type": "text", "text": message.text}]
                for image in message.images:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.media_type};base64,{image.data}"
                            },
                        }
                    )
                messages.append({"role": role, "content": parts})
            else:
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
        slack_channels: list[str] = []
        slack_asked = False  # any slack block at all
        slack_open = False  # a block that named no channel
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

                options = spec.options()
                channel = options.get("channel")
                if "channel" in options and not channel:
                    # Named but empty -- typically `channel: ${SLACK_CHANNEL}`
                    # with the variable unset. Omitting the key asks for every
                    # channel; an empty value is a missing config value, and
                    # reading it as "open" would hand the agent its widest
                    # reach on a typo. Fail boot, as a missing channel did
                    # before the 2026-09-23 ruling.
                    raise BundleError(
                        "slack transport names a 'channel' but its value is empty; "
                        "set it, or omit the key to answer in every invited channel"
                    )
                slack_asked = True
                # `channel` IS OPTIONAL (2026-09-23 operator ruling). A block
                # that names none asks for every channel the app has been
                # invited to -- inviting the app is already the human act
                # that grants the reach, and demanding the ID again is the
                # same permission typed twice.
                if channel:
                    # Collected, not built: every slack block folds into ONE
                    # transport below. Slack Socket Mode load-balances each
                    # event to exactly one open connection per app, so a
                    # transport per channel is not extra coverage -- it is a
                    # lottery where the wrong connection silently drops the
                    # event (2026-09-04, found live).
                    slack_channels.append(str(channel))
                else:
                    slack_open = True
            else:
                warnings.warn(
                    f"transport {spec.kind!r} is not available in this version; skipping",
                    stacklevel=2,
                )
        if slack_asked:
            from daedalus.transports.slack import SlackTransport

            # Mixed blocks resolve OPEN: naming one channel and also asking
            # for any channel is a request for any channel, and the narrower
            # reading would silently ignore the block that asked for more.
            # The first named channel is still passed as `channel`, but on an
            # open transport nothing reads it: it only seeds the tier-2
            # approver, which an open transport does not wire.
            built.append(
                SlackTransport(
                    slack_channels[0] if slack_channels else "",
                    channels=[] if slack_open else slack_channels,
                    escalation_route=self.bundle.spec.approvals.escalation.route,
                )
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
