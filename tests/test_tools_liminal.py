"""LiminalToolProvider — unit tests against a fake client, plus a real
in-process daedalus->liminal integration when liminal is importable."""

import importlib.util
from types import SimpleNamespace

import pytest

from daedalus.tools.liminal import LiminalToolProvider

LIMINAL_INSTALLED = importlib.util.find_spec("liminal") is not None


class FakeOutcome:
    def __init__(self, text, is_error=False):
        self.text = text
        self.is_error = is_error


class FakeLiminalClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def list_tools(self):
        return [
            SimpleNamespace(
                name="scratchpad.set", description="store a note", input_schema={"type": "object"}
            ),
            SimpleNamespace(name="scratchpad.get", description="", input_schema=None),
        ]

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name == "scratchpad.boom":
            return FakeOutcome("handler exploded", is_error=True)
        return FakeOutcome(f"ok: {name}")

    async def aclose(self):
        self.closed = True


async def test_tools_map_to_canonical_specs():
    provider = LiminalToolProvider("http://x", client=FakeLiminalClient())
    specs = await provider.tools()
    assert [s["name"] for s in specs] == ["scratchpad.set", "scratchpad.get"]
    assert specs[1]["parameters"] == {"type": "object", "properties": {}}  # None -> default


async def test_down_gateway_error_names_liminal_and_its_url():
    """Finding #23: 'connection failed' without a WHERE sends the operator
    hunting. The error must say which gateway, at what address, and hint
    at the fix."""

    class DownClient:
        async def list_tools(self):
            raise ConnectionError("All connection attempts failed")

    provider = LiminalToolProvider("http://127.0.0.1:9100", client=DownClient())
    with pytest.raises(RuntimeError, match=r"liminal at http://127.0.0.1:9100.*is it running"):
        await provider.tools()


async def test_call_returns_text_and_error_outcomes_raise():
    client = FakeLiminalClient()
    provider = LiminalToolProvider("http://x", client=client)
    assert await provider.call("scratchpad.set", {"k": "v"}) == "ok: scratchpad.set"
    assert client.calls == [("scratchpad.set", {"k": "v"})]
    with pytest.raises(RuntimeError, match="handler exploded"):
        await provider.call("scratchpad.boom", {})


async def test_aclose_closes_owned_client():
    client = FakeLiminalClient()
    provider = LiminalToolProvider("http://x", client=client)
    await provider.aclose()
    assert client.closed


@pytest.mark.skipif(not LIMINAL_INSTALLED, reason="liminal not installed")
async def test_no_auth_mode_sends_placeholder_not_empty_bearer(monkeypatch):
    """Finding #16: empty token -> 'Bearer ' (trailing space) -> httpx
    'Illegal header value' on real transports. Placeholder when unset."""
    provider = LiminalToolProvider("http://x")
    client = provider._ensure()
    assert client._token() == "no-auth"

    monkeypatch.setenv("LIMINAL_TOKEN", "real-token")
    provider2 = LiminalToolProvider("http://x", token_env="LIMINAL_TOKEN")
    assert provider2._ensure()._token() == "real-token"

    monkeypatch.setenv("LIMINAL_TOKEN", "")
    provider3 = LiminalToolProvider("http://x", token_env="LIMINAL_TOKEN")
    assert provider3._ensure()._token() == "no-auth"


def test_provider_liminal_without_install_fails_at_load(tmp_path):
    if LIMINAL_INSTALLED:
        pytest.skip("liminal is installed; load cannot fail")
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle, BundleError
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: liminal\n  liminal:\n    url: http://x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    with pytest.raises(BundleError, match="liminal is not installed"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM())


@pytest.mark.skipif(not LIMINAL_INSTALLED, reason="liminal not installed")
async def test_family_reunion_daedalus_agent_uses_liminal_gateway(tmp_path, monkeypatch):
    """The full loop, in-process: a daedalus Agent's tool calls flow through
    liminal's real MCP server (ASGI transport, auth disabled) into its
    scratchpad handler and back."""
    import httpx

    monkeypatch.setenv("LIMINAL_AUTH_DISABLED", "1")
    from liminal.client import AsyncMCPClient
    from liminal.server.app import create_app

    app = create_app()  # registry discovered via liminal's own entry points
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://liminal.test"
    )
    client = AsyncMCPClient("http://liminal.test", token_provider=lambda: "")
    client._http = http_client  # use the ASGI-backed client
    client._owned_client = True
    provider = LiminalToolProvider("http://liminal.test", client=client)

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from daedalus.protocols import LLMResponse, ToolCall
    from daedalus.task import Task, TaskState
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  allow: ['scratchpad.*']\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    llm = ScriptedLLM(
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name="scratchpad__set",
                    arguments={"key": "greeting", "value": "hello liminal"},
                )
            ],
        ),
        LLMResponse(
            text="",
            tool_calls=[
                ToolCall(id="2", name="scratchpad__get", arguments={"key": "greeting"})
            ],
        ),
        "the note says hello liminal",
    )
    agent = Agent(Bundle.load(tmp_path), llm=llm, tools=provider)
    task = await agent.handle_task(Task.from_text("store then read a note"))
    assert task.state is TaskState.COMPLETED
    assert task.agent_text() == "the note says hello liminal"
    get_result = llm.calls[2][-1]
    assert get_result["role"] == "tool"
    assert "hello liminal" in get_result["content"]
    await provider.aclose()
