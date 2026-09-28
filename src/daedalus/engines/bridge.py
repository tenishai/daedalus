"""A loopback MCP server that gives an engine running its own agent loop (Claude Code)
the agent's tools and approvals (tenishai/agents#51).

It runs inside the daedalus process, so it has the real approver, gates and tool
providers: a server started as a separate stdio process would have none of them.
Bound to 127.0.0.1 on a port the OS picks, and every request must carry a per-run
bearer token (architect pattern 565b2a210df34ac7, a loopback tool-callback bridge).

It serves:
- the turn's allowlisted tools, under the same model-safe names the LiteLLM engine
  uses, each call going through EngineTurn.call_tool (allowlist, gates, provider);
- `approve`, for Claude Code's --permission-prompt-tool: it receives {tool_name, input}
  and answers {"behavior": "allow", "updatedInput": ...} or {"behavior": "deny",
  "message": ...}. Our own tools are allowed here because call_tool gates them;
  anything else goes through EngineTurn.approve (the bundle's gates and approver).

Checked live against Claude Code 2.1.283 (2026-09-27): --permission-prompt-tool sent
{tool_name: "Bash", input: {command, description}}; "allow" ran the command, "deny"
blocked it, showed the model our message and listed it in permission_denials. Commands
Claude Code treats as read-only (echo) never reach the prompt tool, and the permission
mode must be passed explicitly or the user's default mode applies.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from typing import Any

from daedalus.engines.base import EngineTurn
from daedalus.engines.litellm import _munge

APPROVE_TOOL = "approve"


class _BearerAuth:
    """ASGI wrapper: refuse any HTTP request without the run's token."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # server startup/shutdown, not a request
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        given = headers.get(b"authorization", b"")
        if scope["type"] != "http" or not hmac.compare_digest(
            given, f"Bearer {self.token}".encode()
        ):
            # Anything but an authenticated HTTP request is refused, websockets included.
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
            elif scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


class ToolBridge:
    """Serve one turn's tools and approvals over loopback MCP, for the turn's length.

        async with ToolBridge(turn) as bridge:
            config = bridge.mcp_config()        # for claude --mcp-config
            prompt_tool = bridge.approve_tool   # for --permission-prompt-tool
    """

    def __init__(self, turn: EngineTurn, *, server_name: str | None = None):
        self.turn = turn
        # A fresh name per run: Claude Code names our tools mcp__<server>__<tool>, and a
        # foreign server named "<ours>__files" with a tool "read" would produce exactly
        # our mcp__<ours>__files__read. With an unguessable server name nothing can
        # collide on purpose. The engine that launches Claude Code must also pass
        # --strict-mcp-config so no other server loads. No underscores in the name, so
        # it can't itself split.
        self.server_name = server_name or f"daedalus-{secrets.token_hex(4)}"
        self.token = secrets.token_urlsafe(32)
        self.url: str | None = None
        self._by_munged = {_munge(t["name"]): t for t in turn.tools}
        self._uvicorn = None
        self._task: asyncio.Task | None = None

    @property
    def approve_tool(self) -> str:
        """The name Claude Code knows the approval tool by."""
        return f"mcp__{self.server_name}__{APPROVE_TOOL}"

    def mcp_config(self) -> dict:
        return {
            "mcpServers": {
                self.server_name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {self.token}"},
                }
            }
        }

    # -- MCP handlers --------------------------------------------------------

    async def _list_tools(self, ctx, params):
        from mcp import types

        tools = [
            types.Tool(
                name=munged,
                description=spec.get("description", ""),
                inputSchema=spec.get("parameters") or {"type": "object", "properties": {}},
            )
            for munged, spec in self._by_munged.items()
        ]
        tools.append(
            types.Tool(
                name=APPROVE_TOOL,
                description="Permission prompt handler for this agent's approval gates.",
                inputSchema={
                    "type": "object",
                    "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}},
                    "required": ["tool_name"],
                },
            )
        )
        return types.ListToolsResult(tools=tools)

    async def _call_tool(self, ctx, params):
        from mcp import types

        arguments = params.arguments or {}
        if params.name == APPROVE_TOOL:
            text = json.dumps(await self._decide(arguments))
        else:
            spec = self._by_munged.get(params.name)
            name = spec["name"] if spec else params.name
            self.turn.emit("tool_call", name=name, arguments=arguments)
            text = await self.turn.call_tool(name, arguments)
            self.turn.emit("tool_result", name=name, content=text)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    async def _decide(self, arguments: dict[str, Any]) -> dict:
        tool_name = str(arguments.get("tool_name", ""))
        tool_input = arguments.get("input") or {}
        prefix = f"mcp__{self.server_name}__"
        if tool_name.startswith(prefix) and tool_name[len(prefix):] in self._by_munged:
            # One of the tools this bridge serves: call_tool applies the allowlist and
            # gates on the call itself. Checked by membership, not prefix, so a name that
            # merely starts with ours goes to the gate; an exact collision needs the
            # per-run server name (see __init__).
            return {"behavior": "allow", "updatedInput": tool_input}
        if self.turn.approve is None:
            refusal = f"{tool_name}: nobody can approve it in this run"
            return {"behavior": "deny", "message": refusal}
        refusal = await self.turn.approve(tool_name, tool_input)
        if refusal is None:
            return {"behavior": "allow", "updatedInput": tool_input}
        return {"behavior": "deny", "message": refusal}

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> ToolBridge:
        import uvicorn
        from mcp.server.lowlevel import Server

        server = Server(
            self.server_name, on_list_tools=self._list_tools, on_call_tool=self._call_tool
        )
        app = _BearerAuth(server.streamable_http_app(host="127.0.0.1"), self.token)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
        self._uvicorn = uvicorn.Server(config)
        self._task = asyncio.create_task(self._uvicorn.serve())
        while not self._uvicorn.started:
            if self._task.done():  # failed to start; surface the error
                self._task.result()
            await asyncio.sleep(0.01)
        port = self._uvicorn.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    async def __aexit__(self, *exc) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            await self._task
