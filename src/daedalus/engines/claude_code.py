"""The Claude Code engine: Claude Code is the agent (tenishai/agents#51).

Built on Anthropic's Claude Agent SDK (claude-agent-sdk, MIT), which runs the operator's
own `claude` binary. The bundle's persona is the system prompt; the bundle's tools are
served in-process through the SDK's MCP server, each call going through
EngineTurn.call_tool (allowlist, gates, provider); Claude Code's own built-in tools are
decided by the bundle's gates through the SDK's can_use_tool callback. The engine holds
no policy of its own (architect pattern 7bccb7380ff3cf03): it only forwards to the turn.

Claude Code is the agent doing the work, never a completion endpoint for our loop
(techne knowledge 3555). Shipping this engine for other people's subscriptions waits on
asking Anthropic (the SDK's terms: third parties may not offer claude.ai login).

Settings passed explicitly, and why:
- setting_sources=["user"]: never read the workspace's .claude/settings.json hooks or
  .mcp.json, which a headless run otherwise uses without a trust prompt (agents#76).
- strict_mcp_config: no MCP server loads but ours.
- permission_mode from the bundle: otherwise the operator's own default applies.
- allowed_tools = only our tools (they are gated on the call). Built-ins are not
  pre-approved here, so a built-in call that needs permission reaches can_use_tool and
  the bundle's gates. Claude Code still approves some calls itself, and those never
  reach the gates: read-only commands, what the permission mode auto-approves, and
  permissions.allow rules in the agent's own (user) settings.
- When nobody can approve (turn.can_approve False, the DenyAll approver), gated
  built-ins are withheld from the tool list and the model is told why, as the Agent
  does for its own tools (daedalus PR 105), instead of being offered and refused.
- An EngineTurn built with approve=None (no Agent behind it) gets no callback:
  --permission-prompts none, Claude Code's own unattended mode.
- env: the operator's own (the SDK merges it) plus DAEDALUS_ENGINE_CHILD=1, a recursion
  guard for hooks (architect 343f85aad7317a68). No credential is injected.
"""

from __future__ import annotations

import warnings
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from daedalus.bundle import ClaudeCodeSpec
from daedalus.engines.base import EngineTurn
from daedalus.engines.litellm import _munge
from daedalus.task import ReasonCode, Task, TaskState

SERVER = "daedalus"


def _text_of(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return str(content or "")


def _split(messages: list[dict]) -> tuple[str, str]:
    """(system prompt, prompt): the system message, and the conversation after it.
    A single user message is passed as is; a longer history is written out."""
    has_system = bool(messages) and messages[0]["role"] == "system"
    system = _text_of(messages[0]["content"]) if has_system else ""
    rest = [m for m in messages if m["role"] != "system"]
    if len(rest) <= 1:
        return system, _text_of(rest[0]["content"]) if rest else ""
    lines = ["The conversation so far:"]
    lines += [f"{m['role']}: {_text_of(m['content'])}" for m in rest[:-1]]
    lines += ["", _text_of(rest[-1]["content"])]
    return system, "\n".join(lines)


class ClaudeCodeEngine:
    """Runs a turn as one Claude Code session through the Agent SDK."""

    name = "claude-code"

    def __init__(self, spec: ClaudeCodeSpec, *, query=None):
        self.spec = spec
        self._query = query  # tests pass a fake; None = claude_agent_sdk.query

    def _our_tools(self, turn: EngineTurn, sdk) -> tuple[list, dict[str, str]]:
        """SDK tools for the turn's allowlisted tools, and model-safe -> canonical."""
        canonical: dict[str, str] = {}
        tools = []
        for spec in turn.tools:
            safe = _munge(spec["name"])
            if safe in canonical:
                # Two tools, one model-facing name: refuse rather than pick one
                # (architect pattern bfe06c07e63879ae).
                raise ValueError(
                    f"tools {canonical[safe]!r} and {spec['name']!r} both become {safe!r}"
                )
            canonical[safe] = spec["name"]
            name = spec["name"]

            async def handler(arguments, name=name):
                turn.emit("tool_call", name=name, arguments=arguments)
                result = await turn.call_tool(name, arguments)
                turn.emit("tool_result", name=name, content=result)
                return {"content": [{"type": "text", "text": result}]}

            schema = spec.get("parameters") or {"type": "object", "properties": {}}
            tools.append(sdk.tool(safe, spec.get("description", ""), schema)(handler))
        return tools, canonical

    def _can_use_tool(self, turn: EngineTurn, sdk, ours: set[str]):
        prefix = f"mcp__{SERVER}__"

        async def can_use_tool(tool_name, tool_input, context):
            if tool_name.startswith(prefix) and tool_name[len(prefix):] in ours:
                return sdk.PermissionResultAllow(updated_input=tool_input)  # gated on call
            refusal = await turn.approve(tool_name, tool_input)
            if refusal is None:
                return sdk.PermissionResultAllow(updated_input=tool_input)
            turn.emit("permission_denied", name=tool_name)
            return sdk.PermissionResultDeny(
                message=refusal, interrupt=self.spec.on_denied == "stop"
            )

        return can_use_tool

    def _builtins(self, turn: EngineTurn, system: str) -> tuple[list[str], str]:
        """The built-ins to offer, and the system prompt. When nobody can approve,
        gated built-ins are withheld and the prompt says so."""
        builtins = list(self.spec.builtin_tools)
        if turn.can_approve:
            return builtins, system
        gates = turn.spec.approvals.gates
        withheld = [b for b in builtins if any(fnmatch(b, g.match) for g in gates)]
        if not withheld:
            return builtins, system
        turn.emit("tools_withheld", names=[f"claude.{b}" for b in withheld])
        note = (
            "\n\n## Built-in tools you can't use in this run\n"
            "These need approval, and nobody can give it in this run, so they are not "
            f"available: {', '.join(withheld)}. If the task needs one, say what you "
            "would have done with it."
        )
        return [b for b in builtins if b not in withheld], system + note

    def options(self, turn: EngineTurn, sdk, system: str):
        tools, canonical = self._our_tools(turn, sdk)
        builtins, system = self._builtins(turn, system)
        server = sdk.create_sdk_mcp_server(name=SERVER, tools=tools)
        env = {"DAEDALUS_ENGINE_CHILD": "1"}
        if self.spec.config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(Path(self.spec.config_dir).expanduser())
        kwargs: dict[str, Any] = dict(
            system_prompt=system,
            tools=builtins,
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            setting_sources=["user"],
            permission_mode=self.spec.permission_mode,
            allowed_tools=[f"mcp__{SERVER}__{safe}" for safe in canonical],
            max_turns=turn.spec.tools.max_steps,
            cli_path=self.spec.binary,
            env=env,
        )
        if self.spec.model:
            kwargs["model"] = self.spec.model
        if turn.approve is None:
            kwargs["extra_args"] = {"permission-prompts": "none"}
        else:
            kwargs["can_use_tool"] = self._can_use_tool(turn, sdk, set(canonical))
        return sdk.ClaudeAgentOptions(**kwargs)

    async def run(self, turn: EngineTurn) -> Task:
        task = turn.task
        try:
            import claude_agent_sdk as sdk
        except ImportError:
            return task.reply(
                "The Claude Code engine needs the claude-code extra: "
                "pip install 'daedalus[claude-code]'.",
                state=TaskState.FAILED,
                reason=ReasonCode.ESCALATION,
            )
        system, prompt = _split(turn.messages)
        try:
            options = self.options(turn, sdk, system)
        except ValueError as exc:
            return task.reply(str(exc), state=TaskState.FAILED, reason=ReasonCode.ESCALATION)
        query = self._query or sdk.query
        result = None
        try:
            with warnings.catch_warnings():
                # The SDK warns that our own tools are pre-approved before can_use_tool.
                # That is the design: EngineTurn.call_tool gates them on the call.
                warnings.simplefilter("ignore", sdk.CanUseToolShadowedWarning)
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                        turn.emit("engine_started", engine=self.name)
                    elif isinstance(message, sdk.AssistantMessage):
                        for block in message.content:
                            if isinstance(block, sdk.ThinkingBlock) and block.thinking:
                                turn.emit("reasoning", text=block.thinking)
                            elif isinstance(block, sdk.ToolUseBlock) and not block.name.startswith(
                                f"mcp__{SERVER}__"
                            ):  # ours are traced in the handler
                                turn.emit("tool_call", name=f"claude.{block.name}",
                                          arguments=block.input or {})
                    elif isinstance(message, sdk.ResultMessage):
                        result = message
        except sdk.CLINotFoundError:
            return task.reply(
                f"The Claude Code engine needs `{self.spec.binary}` on PATH, and it isn't "
                "there. Install Claude Code, or run with --engine litellm.",
                state=TaskState.FAILED,
                reason=ReasonCode.ESCALATION,
            )
        except sdk.ClaudeSDKError as exc:
            return task.reply(f"The Claude Code engine failed: {exc}",
                              state=TaskState.FAILED, reason=ReasonCode.ESCALATION)
        return self._reply(task, turn, result)

    def _reply(self, task: Task, turn: EngineTurn, result) -> Task:
        if result is None:
            return task.reply("The Claude Code engine ended without a result.",
                              state=TaskState.FAILED, reason=ReasonCode.ESCALATION)
        # Which models actually ran: a pin or a tier can resolve differently than meant
        # (agents-archive#142, daedalus#103), so the trace records what happened.
        turn.emit("engine_finished", engine=self.name,
                  models=sorted((result.model_usage or {}).keys()), turns=result.num_turns)
        usage = result.usage or {}
        spent = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if spent:
            turn.budget.spend(spent)
        text = str(result.result or "")
        if result.is_error or result.subtype != "success":
            if result.subtype == "error_max_turns":
                return task.reply(
                    "I had to stop before finishing -- this task needed more than the "
                    f"{turn.spec.tools.max_steps} tool steps I'm allowed in one go. A "
                    "narrower ask may fit; my operator can raise the limit.",
                    state=TaskState.FAILED,
                    reason=ReasonCode.POLICY_VIOLATION,
                )
            return task.reply(text or f"The Claude Code engine reported {result.subtype}.",
                              state=TaskState.FAILED, reason=ReasonCode.ESCALATION)
        return task.reply(text)
