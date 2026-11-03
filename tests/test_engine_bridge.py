"""The loopback MCP bridge an engine like Claude Code uses to reach the agent's tools and
approvals (tenishai/agents#51). Exercised over the real HTTP transport, not by calling
handlers directly (architect pattern 0de3a9cb6c1d6f3d)."""

import importlib.metadata
import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("mcp.server.lowlevel", reason="the bridge needs the bridge extra")
if int(importlib.metadata.version("mcp").split(".")[0]) < 2:
    pytest.skip("the bridge needs mcp>=2.2 (the bridge extra)", allow_module_level=True)
pytest.importorskip("uvicorn", reason="the bridge serves over uvicorn")

from daedalus.engines import EngineTurn  # noqa: E402
from daedalus.engines.bridge import ToolBridge  # noqa: E402
from daedalus.protocols import NoBudget, NoCheckpoints  # noqa: E402
from daedalus.task import Task  # noqa: E402
from tests.test_agent_tools import READ_SPEC, make_agent  # noqa: E402


def _turn(tmp_path, *, call_tool=None, approve=None, events=None):
    agent = make_agent(tmp_path, allow=["files.read"])

    async def default_call(name, arguments):
        return f"called {name} with {json.dumps(arguments, sort_keys=True)}"

    return EngineTurn(
        task=Task.from_text("x"),
        messages=[],
        tools=[READ_SPEC],
        call_tool=call_tool or default_call,
        emit=(lambda kind, **f: events.append((kind, f))) if events is not None else (
            lambda *a, **k: None
        ),
        budget=NoBudget(),
        checkpoints=NoCheckpoints(),
        spec=agent.bundle.spec,
        approve=approve,
    )


@asynccontextmanager
async def _client(bridge, token=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    headers = {"Authorization": f"Bearer {token or bridge.token}"}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(bridge.url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def _text(result) -> str:
    return result.content[0].text


async def test_serves_the_turns_tools_and_the_approve_tool_on_loopback(tmp_path):
    async with ToolBridge(_turn(tmp_path)) as bridge:
        assert bridge.url.startswith("http://127.0.0.1:")
        async with _client(bridge) as session:
            names = [t.name for t in (await session.list_tools()).tools]
    assert names == ["files__read", "approve"]


async def test_a_tool_call_goes_through_call_tool_by_canonical_name(tmp_path):
    events = []
    async with ToolBridge(_turn(tmp_path, events=events)) as bridge:
        async with _client(bridge) as session:
            result = await session.call_tool("files__read", {"path": "/x"})
    assert _text(result) == 'called files.read with {"path": "/x"}'
    assert [k for k, _ in events] == ["tool_call", "tool_result"]
    assert events[0][1] == {"name": "files.read", "arguments": {"path": "/x"}}


async def test_requests_without_the_token_are_refused(tmp_path):
    import httpx2

    async with ToolBridge(_turn(tmp_path)) as bridge:
        async with httpx2.AsyncClient() as http:
            no_token = await http.post(bridge.url, json={})
            wrong = await http.post(
                bridge.url, json={}, headers={"Authorization": "Bearer nope"}
            )
    assert no_token.status_code == 401 and wrong.status_code == 401


async def test_approve_allows_our_own_tools_without_asking(tmp_path):
    asked = []

    async def approve(name, arguments):
        asked.append(name)
        return "should not be asked"

    async with ToolBridge(_turn(tmp_path, approve=approve)) as bridge:
        async with _client(bridge) as session:
            result = await session.call_tool(
                "approve",
                {"tool_name": f"mcp__{bridge.server_name}__files__read", "input": {"path": "/x"}},
            )
    assert json.loads(_text(result)) == {"behavior": "allow", "updatedInput": {"path": "/x"}}
    assert asked == []  # call_tool gates our tools on the call itself


async def test_approve_does_not_trust_a_name_that_only_shares_our_prefix(tmp_path):
    """Prudence, PR 107 r1: a tool from another MCP server named e.g. "daedalus__evil"
    reaches Claude Code as mcp__daedalus__evil__Bash. It must go to the gate, not be
    allowed because it starts with our prefix."""
    asked = []

    async def approve(name, arguments):
        asked.append(name)
        return f"blocked: {name}"

    async with ToolBridge(_turn(tmp_path, approve=approve)) as bridge:
        spoof = f"mcp__{bridge.server_name}__evil__Bash"
        async with _client(bridge) as session:
            result = await session.call_tool(
                "approve", {"tool_name": spoof, "input": {"command": "rm -rf ~"}}
            )
    decision = json.loads(_text(result))
    assert decision == {"behavior": "deny", "message": f"blocked: {spoof}"}
    assert asked == [spoof]


async def test_approve_runs_the_gate_for_an_engine_native_tool(tmp_path):
    async def approve(name, arguments):
        return None if arguments.get("command") == "ls" else f"blocked: {name} denied"

    async with ToolBridge(_turn(tmp_path, approve=approve)) as bridge:
        async with _client(bridge) as session:
            ok = await session.call_tool(
                "approve", {"tool_name": "Bash", "input": {"command": "ls"}}
            )
            no = await session.call_tool(
                "approve", {"tool_name": "Bash", "input": {"command": "rm -rf /"}}
            )
    assert json.loads(_text(ok)) == {"behavior": "allow", "updatedInput": {"command": "ls"}}
    assert json.loads(_text(no)) == {"behavior": "deny", "message": "blocked: Bash denied"}


async def test_approve_denies_when_nobody_can_approve(tmp_path):
    async with ToolBridge(_turn(tmp_path, approve=None)) as bridge:
        async with _client(bridge) as session:
            result = await session.call_tool("approve", {"tool_name": "Bash", "input": {}})
    decision = json.loads(_text(result))
    assert decision["behavior"] == "deny" and "nobody can approve" in decision["message"]


async def test_mcp_config_points_claude_at_the_bridge_with_the_token(tmp_path):
    async with ToolBridge(_turn(tmp_path)) as bridge:
        server = bridge.mcp_config()["mcpServers"][bridge.server_name]
        assert server == {
            "type": "http",
            "url": bridge.url,
            "headers": {"Authorization": f"Bearer {bridge.token}"},
        }
        assert bridge.approve_tool == f"mcp__{bridge.server_name}__approve"


async def test_each_bridge_gets_its_own_unpredictable_server_name(tmp_path):
    """Temperance, PR 107 r2: a foreign server named "<ours>__files" exposing "read"
    would be byte-identical to our files__read. A per-run random name (64 bits) means it
    can't be set up in advance. This test checks the names differ and have the right
    shape; the randomness itself comes from secrets.token_hex."""
    async with ToolBridge(_turn(tmp_path)) as a, ToolBridge(_turn(tmp_path)) as b:
        assert a.server_name != b.server_name
        assert a.server_name.startswith("daedalus-") and "_" not in a.server_name
        assert len(a.server_name) == len("daedalus-") + 16


async def test_the_agent_gates_engine_native_tools(tmp_path):
    """Agent._approve_native: a gated built-in goes to the approver; ungated passes."""
    from tests.fakes import RecordingApprover

    gates = "    - match: 'Bash'\n      require: human\n"
    no = make_agent(tmp_path, allow=["files.read"], gates=gates,
                    approver=RecordingApprover(False))
    assert (await no._approve_native("Bash", {"command": "ls"})).startswith(
        "blocked: Bash requires approval (human)"
    )
    assert await no._approve_native("Read", {"file_path": "/x"}) is None
    (tmp_path / "b").mkdir()
    yes_approver = RecordingApprover(True)
    yes = make_agent(tmp_path / "b", allow=["files.read"], gates=gates, approver=yes_approver)
    assert await yes._approve_native("Bash", {"command": "ls"}) is None
    assert yes_approver.asked[0][0] == "Bash"
