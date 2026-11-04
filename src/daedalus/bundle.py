"""The agent bundle — a directory that IS an agent.

agent-card.json (A2A identity) + persona.md + agent.yaml (wiring).
The GUI reads and writes the same files; no code generation.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_persona_comments(persona: str) -> str:
    """Remove HTML comments — author-facing notes never reach the model.

    A persona file is a prompt: everything else in it ships verbatim.
    """
    return _HTML_COMMENT.sub("", persona).strip() + "\n"


class BundleError(Exception):
    """The bundle is missing, malformed, or references undefined variables."""


def interpolate(value, env: dict[str, str] | None = None):
    """Expand ${VAR} / ${VAR:-default} in strings, recursively through dicts/lists."""
    env = os.environ if env is None else env
    if isinstance(value, str):

        def _sub(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise BundleError(f"undefined variable ${{{name}}} (no default given)")

        return _VAR.sub(_sub, value)
    if isinstance(value, dict):
        return {key: interpolate(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, env) for item in value]
    return value


class _WarnExtra(BaseModel):
    """Unknown fields load fine (forward-compat with newer writers) but warn."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _warn_on_extras(self):
        if self.__pydantic_extra__:
            keys = ", ".join(sorted(self.__pydantic_extra__))
            warnings.warn(
                f"{type(self).__name__}: unknown fields ignored by this runtime: {keys}",
                stacklevel=2,
            )
        return self


class BudgetSpec(_WarnExtra):
    per_task_tokens: int | None = None


class ThinkingSpec(_WarnExtra):
    """How much the model may think before it answers, kept apart from the visible
    reply's `max_tokens` (tenishai/daedalus#240). Providers that count thinking against
    `max_tokens` could spend the whole reply budget on it: a one-line answer came back
    as `[reply truncated ...]` twice on the same day. The litellm engine only; the
    provider table in daedalus.llm says what each route is sent."""

    # auto: ask for thinking on models known to reason (Claude 3.7 and later), send
    # nothing elsewhere. on: always ask. off: say so where the route has a way to.
    mode: Literal["auto", "off", "on"] = Field(
        default="auto",
        description="auto asks known reasoning models to think; on always asks; off "
        "turns it off where the route can.",
    )
    # Tokens of thinking the model may spend ON TOP of max_tokens: the request's
    # max_tokens is raised by this much so the visible reply keeps its whole budget.
    # Claude's floor is 1024. Unset: 4096 (daedalus.llm.DEFAULT_THINKING_BUDGET).
    budget_tokens: int | None = Field(
        default=None,
        ge=1024,
        description="Thinking tokens added on top of max_tokens (default 4096, floor 1024).",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_booleans_are_words(cls, value):
        # YAML 1.1 reads a bare `off`/`on` as a boolean, so `thinking: {mode: off}` in a
        # tiers file or agent.yaml arrives here as False. Take it as the word it was.
        if value is False:
            return "off"
        if value is True:
            return "on"
        return value


class ModelSpec(_WarnExtra):
    # A concrete model id, or (after ${VAR} expansion) one of the tier words
    # fast/balanced/best, resolved through DAEDALUS_TIERS (daedalus.tiers,
    # tenishai/daedalus#129). A bundle can say `id: "${MY_MODEL:-best}"`: the default
    # applies only while MY_MODEL is unset, so an operator pins a model by setting it.
    # MY_MODEL set to an empty string expands to "" and fails to load ("model needs an
    # id"); it does not fall back to the tier.
    id: str | None = None
    # The visible reply's ceiling in tokens, sent to the provider as max_tokens (every
    # provider requires one). 8192 bounds cost and fits kobo's 180 s relay timeout; a
    # tier entry may raise it per deployment. Thinking is budgeted separately
    # (`thinking`, tenishai/daedalus#240), so this is the answer's budget alone.
    max_tokens: int = Field(
        default=8192,
        gt=0,
        description="The visible reply's ceiling in tokens; thinking is budgeted "
        "separately under thinking.budget_tokens.",
    )
    # None: a tier entry may fill it; otherwise mode auto with the default budget.
    thinking: ThinkingSpec | None = Field(
        default=None,
        description="Thinking budget, kept apart from max_tokens (litellm engine only).",
    )
    # Seconds one model call may take before it fails (#201), passed to litellm as
    # `timeout`. Without it litellm waits 6000 s. 120 s fits a full max_tokens reply
    # from a hosted model and stays under kobo's 180 s relay timeout. A tier entry
    # fills it when the bundle doesn't set it. The litellm engine only: Claude Code
    # manages its own model calls.
    request_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Seconds one model call may take (litellm engine only).",
    )
    api_base: str | None = None  # OpenAI-compatible endpoints (vLLM, ollama, LM Studio)
    api_key_env: str | None = None  # env var holding a real key; never the key itself
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    @model_validator(mode="after")
    def _id_required(self):
        # model.tier and model.min_tier were removed (tenishai/daedalus#129): the tier
        # now lives in model.id itself. ModelSpec allows unknown fields (forward compat,
        # _WarnExtra), so a bundle still setting either would otherwise load silently
        # with a warning; that's wrong for a field that moved, not one that's merely new.
        removed = sorted({"tier", "min_tier"} & set(self.__pydantic_extra__ or {}))
        if removed:
            raise ValueError(
                f"model.{removed[0]} was removed (tenishai/daedalus#129); put the tier "
                "in model.id instead, e.g. model.id: best"
            )
        if self.id is not None and not self.id.strip():
            self.id = None
        if self.id is None:
            raise ValueError("model needs an id: a model id, or a tier (fast, balanced or best)")
        return self


_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
# Mirrors daedalus.transports.slack.WAKE_MODES; kept here so bundle loading never
# imports the transport module.
_SLACK_WAKE_MODES = frozenset({"mention", "thread", "channel"})


class TransportSpec(BaseModel):
    """kind plus transport-specific fields (channel, port, approvers, ...) kept as extras."""

    model_config = ConfigDict(extra="allow")

    kind: str

    @model_validator(mode="after")
    def _slack_approvers_shape(self) -> TransportSpec:
        """`approvers` on a `kind: slack` block: a list of Slack user ids (#53).

        Checked at load, so a typo fails the bundle instead of silently leaving
        tier-2 with nobody entitled to approve. `[]` is legal and means the same
        as leaving the key out: no approvers, tier-2 denied."""
        if self.kind != "slack":
            return self
        wake = (self.__pydantic_extra__ or {}).get("wake")
        if wake is not None and wake not in _SLACK_WAKE_MODES:
            raise ValueError(
                f"slack transport `wake` must be one of {sorted(_SLACK_WAKE_MODES)}, "
                f"not {wake!r}: mention (only @-mentions in channels), thread (mentions, "
                "then every reply in a thread the agent was asked in at the top), or "
                "channel (every message in the channel)"
            )
        approvers = (self.__pydantic_extra__ or {}).get("approvers")
        if approvers is None:
            return self
        if not isinstance(approvers, list):
            raise ValueError(
                "slack transport `approvers` must be a list of Slack user ids, "
                "e.g. [U012ABC, U034DEF]"
            )
        for entry in approvers:
            if not isinstance(entry, str) or not _SLACK_USER_ID.match(entry):
                raise ValueError(
                    f"slack transport `approvers` entry {entry!r} is not a Slack user id "
                    "(U or W followed by capital letters and digits, e.g. U012ABC)"
                )
        return self

    def options(self) -> dict:
        return dict(self.__pydantic_extra__ or {})


class LiminalSpec(_WarnExtra):
    url: str | None = None  # e.g. http://localhost:9100
    token_env: str | None = None  # env var holding the bearer token; unset = no auth
    # The switchboard's token-exchange base URL (external-MCP arc, C1).
    # When set, token_env holds the agent's STANDING colleague token, and
    # the provider swaps it for a short-lived gateway-audience token
    # before every use -- standing identity never dials the gateway.
    exchange_url: str | None = None


class ColleaguesSpec(_WarnExtra):
    url: str | None = None  # switchboard directory base URL
    token_env: str | None = None  # env var holding the bearer token; unset/empty = degraded
    # Deliberately named `self`: Pydantic v2 model fields never collide with a
    # method's self (BaseModel builds __init__ from the schema, not the name).
    self: str | None = None  # this agent's own name, excluded from directory listings


class GitHubSpec(_WarnExtra):
    """The room PR broker's client half (2026-08-27 spec). A SIDECAR block,
    not a `provider` value: an agent keeps its one primary provider (almost
    always colleagues) and gains github.* alongside it, the same composition
    `builtin` already rides. Absent broker_url = the block is off."""

    broker_url: str | None = None  # the room's own base URL, in-cluster
    token_env: str | None = None  # env var holding the agent's colleague JWT
    # Same deliberate `self` naming as ColleaguesSpec above.
    self: str | None = None  # this agent's own name, for the broker route
    # Head-branch prefix for open_pr when no exact `branch` is given:
    # branches are `<branch_prefix>/<agent>/<slug>`. Self-attribution only.
    branch_prefix: str = "daedalus"

    @field_validator("branch_prefix")
    @classmethod
    def _branch_prefix_is_ref_component(cls, v: str) -> str:
        """Checked at load so a bad prefix fails the bundle, not the first PR."""
        problem = None
        bad_chars = "~^:?*[\\"
        if not v:
            problem = "must not be empty"
        elif any(c.isspace() for c in v):
            problem = "must not contain whitespace"
        elif any(ord(c) < 32 or ord(c) == 127 for c in v):
            problem = "must not contain control characters"
        elif ".." in v or "@{" in v or "//" in v:
            problem = "must not contain '..', '@{' or '//'"
        elif any(c in v for c in bad_chars):
            problem = "must not contain any of ~ ^ : ? * [ \\"
        elif v.startswith("/") or v.endswith("/"):
            problem = "must not start or end with '/'"
        elif v.endswith("."):
            problem = "must not end with '.'"
        else:
            for part in v.split("/"):
                if part.startswith(".") or part.endswith(".lock"):
                    problem = (
                        f"component {part!r} must not start with '.' or end with '.lock'"
                    )
                    break
        if problem:
            raise ValueError(
                f"github.branch_prefix {v!r} is not a valid git branch prefix: {problem} "
                "(e.g. daedalus, or kobo)"
            )
        return v


class CloudTrailSpec(_WarnExtra):
    """A cloud-lane SIDECAR block (2026-09-03 cloud-access ruling): the
    pod's own workload identity federates into an AWS role scoped to one
    CloudTrail Lake event data store -- no stored credential anywhere, and
    the scoping lives in AWS IAM, not here. Stacks alongside the primary
    provider exactly like `github`.

    Two ways to a credential. `role_arn`: the pod assumes the role itself
    with its own cloud identity. `bab_url` + `bab_integration`: the pod asks
    the room's permission service for a one-hour lease, proving itself with
    the service account token projected at `bab_token_file` (audience
    `bab`); the service holds the role, checks what this agent was granted
    and names the agent in the session. With both set the lease wins. With
    neither, the block is off."""

    role_arn: str | None = None  # AWS role trusting this workload's identity
    event_data_store: str | None = None  # Lake event data store id
    region: str = "us-east-2"  # where the lake lives
    group_prefix: str = "engineering_"  # directory-group filter for the query
    bab_url: str | None = None  # the room's permission service; leases replace role_arn
    bab_integration: str | None = None  # which connected account to lease, as bab names it
    bab_token_file: str = "/var/run/secrets/bab/token"  # projected SA token, audience bab

    @property
    def on(self) -> bool:
        leased = bool(self.bab_url and self.bab_integration)
        return bool(self.event_data_store) and bool(self.role_arn or leased)


class SlackReadSpec(_WarnExtra):
    """A read-only SIDECAR block (2026-09-04): the agent may read
    conversation threads from the channels listed here, using the pod's own
    Slack bot token (the transport's env). Slack's channel membership is the
    second wall -- history reads fail until a human invites the bot.

    Three states, and the difference between the last two is deliberate
    (2026-09-23 operator ruling):
      - `channels` absent      -> the block is OFF
      - `channels: []`         -> ON with no allowlist: every channel the app
                                  has been invited to. Inviting the app is
                                  already the human grant; re-typing the IDs
                                  is the same permission asked for twice.
      - `channels: [C012ABC]`  -> ON, and exactly those channels
    """

    # None (absent) and [] (present, empty) must stay distinguishable -- that
    # is the whole off-vs-open distinction above.
    channels: list[str] | None = None  # channel IDs, e.g. C012ABC
    token_env: str = "SLACK_BOT_TOKEN"  # same env the slack transport reads
    scan_limit: int = 2000  # history-scan message budget per search


class JiraSpec(_WarnExtra):
    """The room broker's issue-tracker read lane (2026-09-04). Same custody
    shape as `github`: the room holds the tracker credential, this block
    only names where the broker lives and how the agent proves itself.
    Absent broker_url = the block is off."""

    broker_url: str | None = None  # the room's own base URL, in-cluster
    token_env: str | None = None  # env var holding the agent's colleague JWT
    # Same deliberate `self` naming as ColleaguesSpec above.
    self: str | None = None  # this agent's own name, for the broker route


class AuditTraceSpec(_WarnExtra):
    """`audit.trace` (2026-11-04): the access-provenance evidence chain run in
    a fixed order over the agent's own `cloudtrail` and `slack_read` lanes and
    the gateway's issue-tracker tools, so an auditing agent starts from one
    evidence pack instead of choosing (and sometimes skipping) the steps.
    Everything org-specific lives here. On when the tracker site and project
    are set and both the cloudtrail and slack_read blocks are on."""

    tracker_site: str | None = None  # the tracker tools' cloudId, e.g. yourorg.atlassian.net
    tracker_project: str | None = None  # the access-request project key, e.g. REQ
    tracker_tools: str = "atlassian"  # gateway tool prefix serving the Jira tools
    # Directory-group name prefixes to strip before matching a group against a
    # ticket's words (eng_ops_global-cdn -> global, cdn), and words a ticket
    # need not repeat (a region suffix, say).
    group_prefixes: list[str] = Field(default_factory=list)
    group_noise_words: list[str] = Field(default_factory=list)
    # The words a request bot writes before the Slack thread timestamp in a
    # ticket's description; absent = always search the channel.
    thread_marker: str | None = None
    approval_phrase: str = "Approved by"  # how an approval comment begins
    approver_field: str = "Approver"  # the ticket field naming the approver

    @property
    def on(self) -> bool:
        return bool(self.tracker_site and self.tracker_project)


class WorkspaceSpec(_WarnExtra):
    """Per-task workspaces (#247): a task's metadata names one folder under `parent`."""

    parent: str | None = Field(
        default=None,
        description=(
            "Directory holding the per-task workspaces. A builtin root written as "
            "{workspace} or {workspace}/sub is filled per task from the folder that "
            "task metadata daedalus_workspace names: one directory name, never a path. "
            "A task with no valid workspace fails."
        ),
    )


class ToolsSpec(_WarnExtra):
    provider: Literal["none", "mcp", "liminal", "colleagues"] = "none"
    servers: list[dict] = Field(default_factory=list)
    liminal: LiminalSpec = Field(default_factory=LiminalSpec)
    colleagues: ColleaguesSpec = Field(default_factory=ColleaguesSpec)
    github: GitHubSpec = Field(default_factory=GitHubSpec)
    cloudtrail: CloudTrailSpec = Field(default_factory=CloudTrailSpec)
    slack_read: SlackReadSpec = Field(default_factory=SlackReadSpec)
    jira: JiraSpec = Field(default_factory=JiraSpec)
    audit_trace: AuditTraceSpec = Field(default_factory=AuditTraceSpec)
    # Left an untyped dict on purpose: agent.py dispatches on the group name and passes
    # the block to the provider as keyword args. The description is where a schema
    # reader learns the files-root refusal (tools/builtin.py _refused_root, #199).
    builtin: dict[str, dict] = Field(
        default_factory=dict,
        description=(
            "Built-in tool groups. files: {roots: [...], max_bytes} for files.read, "
            "files.list and files.search; exec: {roots: [...], commands, env, timeout, "
            "max_output} for exec.run, which needs approvals.ceiling.exec: true. "
            "A files root fails the bundle at load when it is /, at or under /proc, "
            "/sys, /dev, /run or /etc, $HOME or a directory above it, or at or under "
            "$TECHNE_HOME. Symlinks are resolved before the check."
        ),
    )
    allow: list[str] = Field(default_factory=list)  # ceiling; empty = deny everything
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    bind: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Tool arguments filled from task metadata on every call (#247), as "
            "{tool: {argument: dotted.metadata.path}}. The model is not shown bound "
            "arguments, and a call whose bound value is missing fails."
        ),
    )
    max_steps: int = 10
    # When the loop runs out of steps or context (#98): "stop" replies with the
    # step/context message; "wrap_up" makes one last call, with no tools offered,
    # for the best partial answer from what was found. The task stays FAILED with
    # the same reason code either way, so callers keying on state see no change.
    on_exhausted: Literal["stop", "wrap_up"] = "stop"
    # Count the loop as exhausted once a model call's prompt reaches this many input
    # tokens, e.g. below a small local server's memory ceiling. None = no limit.
    # A task stopped here fails with 702 like an exhausted budget, but re-funding
    # and resume() would replay the same over-limit call: it needs a larger
    # context_tokens (or a narrower ask), not more budget.
    context_tokens: int | None = Field(default=None, gt=0)
    # Count the loop as exhausted when the model makes the same tool call, with the
    # same arguments, this many times in a row (#100); each repeat before that gets
    # a note on its result. The stop is handled like running out of steps, so
    # on_exhausted applies. None = no limit.
    max_repeats: int | None = Field(default=None, ge=2)
    # Run tool calls the model wrote as text inside <tool_call> or <tools> tags,
    # for models served without native tool calling (Qwen on vLLM). None = on only
    # when model.api_base is set (a self-hosted endpoint). Text salvage skips the
    # model's own decision to call, so a reply quoting a caller's tagged JSON runs
    # it (#200); turn it off for a self-hosted model that calls tools natively.
    salvage_text_calls: bool | None = Field(
        default=None,
        description=(
            "Run tool calls written as <tool_call>/<tools> text. "
            "null = on only when model.api_base is set."
        ),
    )


#: Revision policies this runtime knows how to run. "operator" promotes only
#: candidates whose surviving fact was taught by the operator (source
#: starting with intake:chat-); everything else stays pending for a human.
_KNOWN_REVISION_POLICIES = ("operator",)


class MemorySpec(_WarnExtra):
    personal: Literal["none", "techne"] = "none"
    home: str | None = None  # techne home dir; default is techne's own (~/.techne)
    # Reserved (#111): only "none" is accepted. "outbox" used to validate and do
    # nothing, because the team sync outbox (docs/design.md section 7) was never
    # built; it is rejected below rather than silently ignored.
    team: Literal["none"] = "none"
    # Post-boot belief revision (daedalus.memory.revision): a background pass
    # that lets the agent's own model judge techne's staged contradiction
    # candidates. None (absent, the default) never constructs the pass at all
    # -- a bundle that hasn't opted in gets byte-identical runtime behavior to
    # a version of daedalus that has never heard of revision. "operator" is
    # the only implemented policy; any other value is not a crash -- it warns
    # and disables, same as an unknown top-level field.
    revision: str | None = None

    @field_validator("team", mode="before")
    @classmethod
    def _reject_unbuilt_team_outbox(cls, value):
        if value == "outbox":
            raise ValueError(
                "memory.team: outbox is not implemented yet (the team sync outbox "
                "was never built), so it would do nothing; remove it or set "
                "`team: none`"
            )
        return value

    @model_validator(mode="after")
    def _validate_revision(self):
        if self.revision is not None and self.revision not in _KNOWN_REVISION_POLICIES:
            warnings.warn(
                f"memory.revision {self.revision!r} is not a known policy "
                f"(known: {', '.join(_KNOWN_REVISION_POLICIES)}); disabling",
                stacklevel=2,
            )
            self.revision = None
        return self


class CeilingSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    write: bool | None = None
    hosts: list[str] | None = None
    # The bundle's consent to run commands: tools.builtin.exec loads only when true.
    exec: bool | None = None


class GateSpec(_WarnExtra):
    match: str
    require: str = "plan_approval"


class EscalationSpec(_WarnExtra):
    route: str = "cli"


class ApprovalsSpec(_WarnExtra):
    ceiling: CeilingSpec = Field(default_factory=CeilingSpec)
    gates: list[GateSpec] = Field(default_factory=list)
    escalation: EscalationSpec = Field(default_factory=EscalationSpec)


class ClaudeCodeSpec(_WarnExtra):
    """Settings for the Claude Code engine (tenishai/agents#51). Used only when the
    operator runs the bundle with that engine; the LiteLLM engine ignores them."""

    # None = the Claude Code CLI bundled with claude-agent-sdk, else `claude` on PATH
    # (the SDK's own lookup). Set a path or name only to run a different binary.
    binary: str | None = None
    # A full model id. None = whatever the Claude Code config dir pins in its
    # settings.json. Never an alias, which can start resolving to a different model.
    model: str | None = None
    # Claude Code built-ins this agent may use (e.g. Read, Grep, Bash). Default none: the
    # agent's tools come from the bundle. A built-in call that needs permission goes
    # through the bundle's approval gates, which name it claude.<Tool> (match:
    # "claude.Bash", as the trace shows it); one with no matching gate is allowed.
    # Calls Claude Code approves itself never reach the gates, for example read-only
    # tools and commands, what the permission mode auto-approves, and permissions.allow
    # rules or PreToolUse hooks in the agent's own (user) settings. To be sure a
    # built-in can't run, leave it out of this list.
    builtin_tools: list[str] = Field(default_factory=list)
    # Passed explicitly: otherwise the operator's own default mode applies. "default"
    # asks (through our approval gates) for anything not pre-approved.
    permission_mode: Literal["default", "dontAsk", "acceptEdits", "plan"] = "default"
    # When our gates deny a built-in tool: "continue" tells the model and lets it go
    # on; "stop" ends the run. An explicit choice, never a fall-through (architect
    # pattern cfa4619c74d518cb).
    on_denied: Literal["continue", "stop"] = "continue"
    # CLAUDE_CONFIG_DIR for the child. None = inherit the operator's environment.
    config_dir: str | None = None
    # The directory Claude Code works in (tenishai/daedalus#242). None = wherever
    # `daedalus run` started. `~` is expanded; a path that isn't a directory fails the
    # load when this engine runs the bundle. Claude Code auto-approves read-only tools
    # inside it, so it widens what runs without reaching the gates; the workspace's own
    # .claude settings and .mcp.json still never load (setting_sources=["user"]).
    cwd: str | None = None


class EngineSpec(_WarnExtra):
    claude_code: ClaudeCodeSpec = Field(default_factory=ClaudeCodeSpec)


class AgentSpec(_WarnExtra):
    """Schema of agent.yaml. Approvals are parsed in v0.1, enforced from v0.2."""

    name: str
    card: str = "agent-card.json"
    persona: str = "persona.md"
    model: ModelSpec
    transports: list[TransportSpec] = Field(default_factory=lambda: [TransportSpec(kind="cli")])
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    checkpoints: Literal["none", "sqlite"] = "none"
    approvals: ApprovalsSpec = Field(default_factory=ApprovalsSpec)
    # Per-engine settings. WHICH engine runs the bundle is the operator's choice at
    # run time (daedalus run --engine), not the bundle's: the same bundle must run for
    # someone with Claude Code and someone without.
    engine: EngineSpec = Field(default_factory=EngineSpec)
    # Seconds a task's engine run may take, model calls and tool calls together
    # (#201). Past it the run is cancelled, tool calls included, and the task fails
    # with reason 704. Default 600 s: ten tool steps of ordinary model and tool
    # latency fit, and a stalled one still ends in minutes, not hours.
    task_timeout: float = Field(
        default=600.0,
        gt=0,
        description="Seconds a task's engine run may take; past it the task fails 704.",
    )


#: The placeholder syntax `interpolate()` (above) expands, anchored to match a whole
#: field value rather than `_VAR`'s substring search: a non-string field must be
#: written as ONE placeholder (`max_tokens: "${X:-16384}"`), never text plus a
#: placeholder, since anything else can't parse as that field's type once expanded.
_PLACEHOLDER_PATTERN = r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}$"

#: Keys that describe a field rather than constrain its value. Kept as siblings of
#: the widened `anyOf`, matching how pydantic already renders an optional field
#: (`{"anyOf": [...], "default": null, "title": "..."}`) rather than nesting them
#: inside a branch, where a schema-reading editor wouldn't look for them.
_SCHEMA_METADATA_KEYS = ("title", "description", "default", "examples", "deprecated")


def _placeholder_eligible(node: dict) -> bool:
    """A leaf that `${VAR}` expansion could reach but a plain string couldn't already
    satisfy: an enum (Literal) or a bare integer/number/boolean. A string field
    already accepts a placeholder as ordinary text -- nothing to widen there."""
    if "$ref" in node:
        return False  # a nested submodel, not a scalar
    if "enum" in node:
        return True
    return node.get("type") in ("integer", "number", "boolean")


def _extend_field_schema(field_schema: dict) -> None:
    """Let one field's schema also accept a `${VAR}` / `${VAR:-default}` placeholder,
    in place. A field already written as `anyOf` (nullable) gets a sibling branch;
    a plain scalar field is wrapped into one, with its metadata kept outside it."""
    if "anyOf" in field_schema:
        branches = field_schema["anyOf"]
        if any(_placeholder_eligible(b) for b in branches):
            branches.append({"type": "string", "pattern": _PLACEHOLDER_PATTERN})
        return
    if not _placeholder_eligible(field_schema):
        return
    metadata = {k: field_schema.pop(k) for k in _SCHEMA_METADATA_KEYS if k in field_schema}
    branches = [dict(field_schema), {"type": "string", "pattern": _PLACEHOLDER_PATTERN}]
    field_schema.clear()
    field_schema["anyOf"] = branches
    field_schema.update(metadata)


def _allow_placeholders(schema: dict) -> None:
    """Widen every non-string scalar field in `schema`, in place (tenishai/daedalus#130):
    `Bundle.load` expands `${VAR}` placeholders before validating (`interpolate()`,
    above), so the published schema must describe what daedalus actually accepts —
    a placeholder in any integer, boolean or enum field, not just string ones."""
    for defn in schema.get("$defs", {}).values():
        for field_schema in defn.get("properties", {}).values():
            _extend_field_schema(field_schema)
    for field_schema in schema.get("properties", {}).values():
        _extend_field_schema(field_schema)


def bundle_schema() -> dict:
    """The canonical agent.yaml shape as JSON Schema — the published contract
    for consumers that can't import Python (cabal's bundle reader, editors).

    The runtime is the source of truth: this is emitted from AgentSpec, and
    a test pins docs/bundle.schema.json to this output so the committed
    artifact cannot drift. Regenerate with `daedalus schema`.
    """
    import daedalus  # function-local: daedalus/__init__ imports this module

    schema = {
        **AgentSpec.model_json_schema(),
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/tenishai/daedalus/blob/main/docs/bundle.schema.json",
        "title": "daedalus agent bundle (agent.yaml)",
        "version": daedalus.__version__,
        "description": (
            "Wiring for a daedalus agent bundle — a directory that IS an "
            "agent (agent.yaml + persona.md + agent-card.json). Unknown "
            "fields load with a warning, never silently. A non-string field may "
            "also be written as a ${VAR} / ${VAR:-default} placeholder, expanded "
            "from the environment before this shape applies."
        ),
    }
    _allow_placeholders(schema)
    # model.tier and model.min_tier were removed (#129) and load refuses them, but
    # ModelSpec allows unknown keys, so say so here too: a consumer validating against
    # this schema (kobo's vendored copy) then refuses what Bundle.load refuses.
    schema["$defs"]["ModelSpec"]["not"] = {
        "anyOf": [{"required": ["tier"]}, {"required": ["min_tier"]}]
    }
    return schema



#: Leading YAML frontmatter in a SKILL.md. Same dialect docker/seeder.py strips
#: before seeding (`_FRONTMATTER` there) -- deliberately the same shape, read by
#: two readers for two different purposes. The seeder drops it because catalog
#: metadata is not procedure and has no business in a memory entry. Here it is
#: the whole point: `when_to_use` is what the index in the system prompt is made
#: of, and `requires` is what the load-time precondition check reads.
_SKILL_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    """One `skills/<id>/SKILL.md`, parsed but not loaded into the prompt.

    `body` is held rather than injected: a catalogue's worth of bodies cannot go
    in a system prompt and grows without bound as skills are added. One reporting
    team measured their own fourteen-skill assignment at ~183 KB (#57) -- their
    figure for their catalogue, not a property of this runtime, and cited only to
    show the order of magnitude. Only `id` and `when_to_use` are indexed; the body
    is fetched on demand through the `skills.load` tool. (#58)
    """

    id: str
    body: str
    name: str | None = None
    when_to_use: str | None = None
    requires: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """The one index line. Falls back through when_to_use -> name -> id, so
        a skill with no frontmatter at all is still addressable by name rather
        than silently missing from the index."""
        return self.when_to_use or self.name or self.id


def parse_skill(skill_id: str, path: Path) -> Skill:
    """Read one SKILL.md into a Skill. Malformed frontmatter degrades to "no
    frontmatter" rather than raising: a skill whose YAML we cannot parse is
    still a usable procedure body, and refusing to boot the whole agent over a
    catalog-metadata typo would be a worse failure than an unindexed skill."""
    text = path.read_text()
    meta: dict = {}
    match = _SKILL_FRONTMATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
            if isinstance(loaded, dict):
                meta = loaded
            else:
                warnings.warn(
                    f"{path}: frontmatter is not a mapping; ignoring", stacklevel=2
                )
        except yaml.YAMLError as exc:
            warnings.warn(f"{path}: unparseable frontmatter ({exc}); ignoring", stacklevel=2)
        text = text[match.end():]

    # Non-list shapes reach the isinstance check and degrade with a warning.
    # FALSY ones (False, 0, "", {}, []) never get that far -- `or []` short
    # circuits above it and they degrade SILENTLY, which is correct but is not
    # the same thing as being handled here.
    #
    # PyYAML is YAML 1.1, so an unquoted `requires: yes` is the BOOLEAN True.
    # Three adjacent typos used to fail three different ways:
    #   requires: no       -> False, short-circuits, silently "no requirements"
    #   requires: yes      -> True, reached the comprehension below and took the
    #                         whole agent down with a bare TypeError naming
    #                         neither the file nor the field
    #   requires: {a: b}   -> a dict IS iterable, so it yielded ["a"] from key
    #                         iteration -- a PHANTOM REQUIREMENT that would then
    #                         be reported unmet against the allowlist. Silent
    #                         corruption, and the worst of the three.
    # That contradicted this function's own degradation contract on exactly the
    # input it was written for: SKILL.md files are authored outside this repo.
    requires = meta.get("requires") or []
    if not isinstance(requires, list):
        if isinstance(requires, str):  # `requires: gateway.logs`, not a list
            requires = [requires]
        else:
            warnings.warn(
                f"{path}: 'requires' is not a list or a string; ignoring", stacklevel=2
            )
            requires = []

    return Skill(
        id=skill_id,
        body=text.strip(),
        name=meta.get("name"),
        when_to_use=meta.get("when_to_use"),
        requires=[str(item) for item in requires],
        meta=meta,
    )


def load_skills(root: Path) -> dict[str, Skill]:
    """Every `skills/<id>/SKILL.md` under `root`, keyed by directory name.

    Same one-level glob the boot seeder uses, so the agent process's view of
    which skills exist cannot disagree with what that seeder put into memory."""
    skills_dir = root / "skills"
    return {
        md.parent.name: parse_skill(md.parent.name, md)
        for md in sorted(skills_dir.glob("*/SKILL.md"))
    }


def unmet_requirements(skills: dict[str, Skill], allow: list[str]) -> dict[str, list[str]]:
    """Skills whose `requires:` names tools the allowlist does not admit.

    Matched with fnmatch because `tools.allow` holds PATTERNS, not names --
    `gateway.*` satisfies a skill requiring `gateway.logs`, and comparing the
    raw strings would report every globbed allowlist as entirely unmet.

    This is the allowlist ceiling, not the provider's live listing: a tool the
    allowlist admits but no provider offers is a different gap and is not
    detectable here. Reporting what IS detectable beats reporting nothing,
    which is what happens today. (#59)
    """
    unmet = {}
    for skill_id, skill in skills.items():
        missing = [
            required
            for required in skill.requires
            if not any(fnmatch(required, pattern) for pattern in allow)
        ]
        if missing:
            unmet[skill_id] = missing
    return unmet


@dataclass
class Bundle:
    path: Path
    spec: AgentSpec
    card: dict
    persona: str
    skills: dict[str, Skill] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path, env: dict[str, str] | None = None) -> Bundle:
        root = Path(path)
        wiring = root / "agent.yaml"
        if not wiring.is_file():
            raise BundleError(f"not an agent bundle: {wiring} does not exist")

        raw = yaml.safe_load(wiring.read_text()) or {}
        if not isinstance(raw, dict):
            raise BundleError(f"{wiring}: expected a mapping at top level")
        spec = AgentSpec.model_validate(interpolate(raw, env))

        card_file = root / spec.card
        persona_file = root / spec.persona
        for required in (card_file, persona_file):
            if not required.is_file():
                raise BundleError(f"bundle references missing file: {required}")

        try:
            card = json.loads(card_file.read_text())
        except json.JSONDecodeError as exc:
            raise BundleError(f"{card_file}: invalid JSON — {exc}") from exc

        return cls(
            path=root,
            spec=spec,
            card=card,
            persona=strip_persona_comments(persona_file.read_text()),
            skills=load_skills(root),
        )
