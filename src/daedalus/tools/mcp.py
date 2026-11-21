"""Direct-MCP tool provider — stdio servers declared in agent.yaml.

Tool names are canonical `server.tool`. Requires the `mcp` extra:
pip install 'tenish-daedalus[mcp]'. The liminal gateway provider arrives in v0.3.

Session lifecycle is owned by a dedicated worker task: MCP's stdio client
opens anyio cancel scopes that must enter and exit in the SAME task, and
REPL cancellation (Ctrl-C) or interpreter shutdown would otherwise tear
them down cross-task (dogfood finding #13).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from daedalus.protocols import ToolResult


class MCPError(RuntimeError):
    pass


def _argv(name: str, server: dict) -> list[str]:
    """The server's argv. `command` is a list, or a string plus an optional `args`
    list, as most MCP client configs write it (daedalus#117)."""
    command, args = server["command"], server.get("args", [])
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not all(isinstance(a, str) for a in command):
        raise MCPError(f"mcp server {name!r}: 'command' must be a string or a list of strings")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise MCPError(f"mcp server {name!r}: 'args' must be a list of strings")
    return [*command, *args]


def result_text(content) -> str:
    """A tool result as text for the model: every text block, and the text of
    every embedded resource (an `EmbeddedResource`: a file or document the
    server returns inline, `.resource.text` with a uri and a mime type). A
    text-only flattening handed the model a server's "successfully downloaded"
    line and lost the file it came with (GitHub's hosted MCP server, found by
    the kobo wire test 2026-10-31; the same fix is liminal 0.9.3's
    `ToolCallOutcome.text`). A resource is introduced by one line naming its
    uri and type; a binary resource (`blob`) is named and not expanded."""
    parts: list[str] = []
    for c in content or ():
        text = getattr(c, "text", None)
        res = getattr(c, "resource", None)
        if isinstance(text, str) and text:
            parts.append(text)
        elif res is not None:
            uri = getattr(res, "uri", None) or ""
            mime = getattr(res, "mimeType", None) or ""
            head = f"[resource {uri}" + (f" ({mime})" if mime else "") + "]"
            body = getattr(res, "text", None)
            if isinstance(body, str):
                parts.append(f"{head}\n{body}")
            elif getattr(res, "blob", None) is not None:
                parts.append(f"{head} binary, not shown")
    return "\n".join(parts) if parts else "(no text content)"


def result_is_empty(content) -> bool:
    """Did the server return nothing the model could use (#268)? Judged from
    the blocks, not from result_text's "(no text content)" stand-in: empty =
    no blocks, or only text blocks that are blank. Any other block (a
    resource, an image) is content."""
    for block in content or ():
        if getattr(block, "type", None) != "text":
            return False
        text = getattr(block, "text", None)
        if not isinstance(text, str) or text.strip():
            return False
    return True


def _field(obj, new: str, old: str):
    """mcp 2.x renamed camelCase fields (inputSchema -> input_schema, isError ->
    is_error); read whichever this SDK has."""
    return getattr(obj, new, None) if hasattr(obj, new) else getattr(obj, old, None)


class MCPToolProvider:
    """One stdio session per configured server, all started (and stopped)
    inside a single worker task on first use.

    servers: [{name: files, command: ["npx", "@modelcontextprotocol/..."],
               env: {...}}, ...]  — `name` defaults to server<N>.
    """

    def __init__(self, servers: list[dict]):
        self._configs: dict[str, dict] = {}
        for index, server in enumerate(servers):
            name = server.get("name", f"server{index}")
            if not server.get("command"):
                raise MCPError(f"mcp server {name!r}: 'command' is required")
            self._configs[name] = {**server, "command": _argv(name, server)}
        self._sessions: dict = {}
        self._worker: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._shutdown: asyncio.Event | None = None
        self._boot_error: BaseException | None = None

    async def _run(self) -> None:
        """The worker: enter every session, hold, exit — all in this task."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            async with AsyncExitStack() as stack:
                for name, config in self._configs.items():
                    command, *args = config["command"]
                    params = StdioServerParameters(
                        command=command, args=args, env=config.get("env")
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    self._sessions[name] = session
                self._ready.set()
                await self._shutdown.wait()
        except BaseException as exc:
            self._boot_error = exc
            raise
        finally:
            self._sessions.clear()
            self._ready.set()  # unblock waiters even on failure

    async def _ensure_started(self) -> None:
        if self._worker is None:
            self._ready = asyncio.Event()
            self._shutdown = asyncio.Event()
            self._worker = asyncio.create_task(self._run(), name="mcp-sessions")
        await self._ready.wait()
        if self._boot_error is not None:
            if isinstance(self._boot_error, ImportError):
                raise MCPError(
                    "mcp is not installed — pip install 'tenish-daedalus[mcp]'"
                ) from self._boot_error
            raise MCPError(f"mcp sessions failed to start: {self._boot_error}")

    async def tools(self) -> list[dict]:
        await self._ensure_started()
        specs: list[dict] = []
        for name, session in self._sessions.items():
            listed = await session.list_tools()
            for tool in listed.tools:
                specs.append(
                    {
                        "name": f"{name}.{tool.name}",
                        "description": tool.description or "",
                        "parameters": _field(tool, "input_schema", "inputSchema")
                        or {"type": "object", "properties": {}},
                    }
                )
        return specs

    async def call(self, name: str, payload: dict) -> str:
        await self._ensure_started()
        server, _, tool = name.partition(".")
        if server not in self._sessions or not tool:
            raise MCPError(f"unknown tool {name!r}")
        session = self._sessions[server]
        result = await session.call_tool(tool, payload)
        text = result_text(result.content)
        if _field(result, "is_error", "isError"):
            raise MCPError(text)
        return ToolResult(text, "empty" if result_is_empty(result.content) else "ok")

    async def aclose(self) -> None:
        if self._worker is None:
            return
        self._shutdown.set()
        try:
            await self._worker
        except BaseException:
            pass  # teardown must never mask the real work
        self._worker = None
        self._boot_error = None
