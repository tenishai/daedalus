"""Direct-MCP tool provider — stdio servers declared in agent.yaml.

Tool names are canonical `server.tool`. Requires the `mcp` extra:
pip install daedalus[mcp]. The limenal gateway provider arrives in v0.3.
"""

from __future__ import annotations

from contextlib import AsyncExitStack


class MCPError(RuntimeError):
    pass


class MCPToolProvider:
    """One stdio session per configured server, started lazily on first use.

    servers: [{name: files, command: ["npx", "@modelcontextprotocol/..."],
               env: {...}}, ...]  — `name` defaults to server<N>.
    """

    def __init__(self, servers: list[dict]):
        self._configs: dict[str, dict] = {}
        for index, server in enumerate(servers):
            name = server.get("name", f"server{index}")
            if not server.get("command"):
                raise MCPError(f"mcp server {name!r}: 'command' is required")
            self._configs[name] = server
        self._sessions: dict = {}
        self._stack: AsyncExitStack | None = None

    async def _session(self, name: str):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover
            raise MCPError("mcp is not installed — pip install 'daedalus[mcp]'") from exc

        if name in self._sessions:
            return self._sessions[name]
        config = self._configs[name]
        if self._stack is None:
            self._stack = AsyncExitStack()
        command, *args = config["command"]
        params = StdioServerParameters(command=command, args=args, env=config.get("env"))
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session
        return session

    async def tools(self) -> list[dict]:
        specs: list[dict] = []
        for name in self._configs:
            session = await self._session(name)
            listed = await session.list_tools()
            for tool in listed.tools:
                specs.append(
                    {
                        "name": f"{name}.{tool.name}",
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}},
                    }
                )
        return specs

    async def call(self, name: str, payload: dict) -> str:
        server, _, tool = name.partition(".")
        if server not in self._configs or not tool:
            raise MCPError(f"unknown tool {name!r}")
        session = await self._session(server)
        result = await session.call_tool(tool, payload)
        parts = [c.text for c in result.content if getattr(c, "text", None)]
        text = "\n".join(parts) if parts else "(no text content)"
        if getattr(result, "isError", False):
            raise MCPError(text)
        return text

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()
