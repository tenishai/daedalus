"""MCPToolProvider against a real stdio MCP server, not a stub (daedalus#117).

The provider read mcp 1.x field names (inputSchema, isError), which mcp 2.x renamed,
and no test ran a real server, so the break went unnoticed once the claude-code extra
brought in mcp>=2.2. These run whichever mcp is installed.
"""

import sys

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed")

from daedalus.tools.mcp import MCPError, MCPToolProvider  # noqa: E402

SERVER = '''
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

server = MCPServer("t")

@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"

@server.tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("it broke")

server.run("stdio")
'''


@pytest.fixture
def server_script(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(SERVER)
    return str(path)


async def _exercise(provider):
    try:
        specs = {s["name"]: s for s in await provider.tools()}
        assert set(specs) == {"t.echo", "t.boom"}
        assert "text" in specs["t.echo"]["parameters"]["properties"]
        assert await provider.call("t.echo", {"text": "hi"}) == "echo: hi"
        with pytest.raises(MCPError, match="it broke|boom"):  # mcp 2.x hides the text
            await provider.call("t.boom", {})
    finally:
        await provider.aclose()


async def test_a_real_server_lists_calls_and_reports_errors(server_script):
    await _exercise(MCPToolProvider([{"name": "t", "command": [sys.executable, server_script]}]))


async def test_a_string_command_with_args_is_one_argv(server_script):
    await _exercise(
        MCPToolProvider([{"name": "t", "command": sys.executable, "args": [server_script]}])
    )


@pytest.mark.parametrize("server", [
    {"name": "t", "command": 42},
    {"name": "t", "command": ["python3", 7]},
    {"name": "t", "command": "python3", "args": "server.py"},
])
def test_a_malformed_command_is_refused_at_load(server):
    with pytest.raises(MCPError, match="must be"):
        MCPToolProvider([server])
