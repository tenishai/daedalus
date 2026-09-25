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
    'Illegal header value' on real transports. Placeholder when unset.
    The placeholder is set by _refresh_bearer, which tools() and call()
    run before every use, so the real client is read after one."""
    provider = LiminalToolProvider("http://x")
    await provider._refresh_bearer()
    assert provider._ensure()._token() == "no-auth"

    monkeypatch.setenv("LIMINAL_TOKEN", "real-token")
    provider2 = LiminalToolProvider("http://x", token_env="LIMINAL_TOKEN")
    await provider2._refresh_bearer()
    assert provider2._ensure()._token() == "real-token"

    monkeypatch.setenv("LIMINAL_TOKEN", "")
    provider3 = LiminalToolProvider("http://x", token_env="LIMINAL_TOKEN")
    await provider3._refresh_bearer()
    assert provider3._ensure()._token() == "no-auth"


@pytest.mark.parametrize("token_env", [None, "LIM_TOKEN"])
async def test_static_mode_empty_token_becomes_placeholder_bearer(monkeypatch, token_env):
    """Finding #16 without liminal installed (CI): an unset or empty token
    becomes the "no-auth" placeholder bearer before the first call. The
    fake client never sees the header, so this pins the bearer the real
    client's token_provider reads, not the header itself."""
    monkeypatch.setenv("LIM_TOKEN", "")
    provider = LiminalToolProvider("http://gw", token_env=token_env, client=FakeLiminalClient())
    await provider.call("scratchpad.set", {})
    assert provider._bearer == "no-auth"


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


# ---- the `spec.liminal.*` wiring itself -----------------------------------
# These two exist because the wiring below `import liminal` in
# Agent._tools_from_spec ran in NEITHER configuration: without liminal the
# import guard raises first, and with liminal the test above skips itself. A
# missed rename on `spec.liminal.url` / `spec.liminal.token_env` would have
# shipped an AttributeError behind a fully green suite. Found by Prudence
# reviewing the limenal->liminal rename.


@pytest.mark.skipif(not LIMINAL_INSTALLED, reason="liminal not installed")
def test_provider_liminal_without_url_is_a_clean_bundle_error(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle, BundleError
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: liminal\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    with pytest.raises(BundleError, match="requires tools.liminal.url"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM())


@pytest.mark.skipif(not LIMINAL_INSTALLED, reason="liminal not installed")
def test_spec_url_and_token_env_reach_the_provider(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from daedalus.tools.liminal import LiminalToolProvider
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: liminal\n"
        "  liminal:\n    url: http://gateway.test\n    token_env: LIMINAL_TOKEN\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")

    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM())

    assert isinstance(agent.tools, LiminalToolProvider)
    assert agent.tools.url == "http://gateway.test"
    assert agent.tools.token_env == "LIMINAL_TOKEN"


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


# --- the gateway token exchange (external-MCP arc, C1) ------------------------


class _ExchangeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeExchange:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []  # (url, headers)

    async def post(self, url, headers=None):
        self.posts.append((url, dict(headers or {})))
        return self.responses.pop(0)

    async def aclose(self):
        pass


def _exchanging(monkeypatch, responses, colleague="colleague-jwt"):
    if colleague is None:
        monkeypatch.delenv("CABAL_COLLEAGUE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CABAL_COLLEAGUE_TOKEN", colleague)
    return LiminalToolProvider(
        "http://gw",
        token_env="CABAL_COLLEAGUE_TOKEN",
        client=FakeLiminalClient(),
        exchange_url="http://cabal/",
        exchange_client=FakeExchange(responses),
    )


def _minted(expires_in_s):
    from datetime import UTC, datetime, timedelta

    return _ExchangeResp(200, {
        "access_token": "liminal-token",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in_s)).isoformat(),
    })


async def test_exchange_swaps_the_colleague_token_and_caches(monkeypatch):
    provider = _exchanging(monkeypatch, [_minted(900)])
    await provider.call("scratchpad.set", {"k": "v"})
    await provider.call("scratchpad.get", {})
    ex = provider._exchange_client
    # One exchange serves both calls; the colleague token authenticated it,
    # on the switchboard's exchange route.
    assert len(ex.posts) == 1
    url, headers = ex.posts[0]
    assert url == "http://cabal/auth/liminal-token"
    assert headers["Authorization"] == "Bearer colleague-jwt"
    assert provider._bearer == "liminal-token"


async def test_near_expiry_re_exchanges(monkeypatch):
    provider = _exchanging(monkeypatch, [_minted(30), _minted(900)])
    await provider.call("scratchpad.set", {})
    await provider.call("scratchpad.get", {})
    assert len(provider._exchange_client.posts) == 2


async def test_refused_exchange_names_the_switchboard(monkeypatch):
    provider = _exchanging(monkeypatch, [_ExchangeResp(403)])
    with pytest.raises(RuntimeError, match="http://cabal.*403"):
        await provider.call("scratchpad.set", {})


async def test_missing_colleague_token_reads_as_wiring_error(monkeypatch):
    provider = _exchanging(monkeypatch, [], colleague=None)
    with pytest.raises(RuntimeError, match="CABAL_COLLEAGUE_TOKEN"):
        await provider.tools()


async def test_unreadable_expiry_fails_safe_toward_re_exchange(monkeypatch):
    bad = _ExchangeResp(200, {"access_token": "t1", "expires_at": "whenever"})
    provider = _exchanging(monkeypatch, [bad, _minted(900)])
    await provider.call("scratchpad.set", {})
    await provider.call("scratchpad.get", {})
    assert len(provider._exchange_client.posts) == 2


async def test_static_mode_reads_env_once_and_never_exchanges(monkeypatch):
    monkeypatch.setenv("LIM_TOKEN", "static-bearer")
    provider = LiminalToolProvider(
        "http://gw", token_env="LIM_TOKEN", client=FakeLiminalClient()
    )
    await provider.call("scratchpad.set", {})
    assert provider._bearer == "static-bearer"
    assert provider._exchange_client is None


def test_sidecar_stacks_beside_a_primary(tmp_path, monkeypatch):
    import sys
    import types

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    monkeypatch.setitem(sys.modules, "liminal", types.ModuleType("liminal"))
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: none\n"
        "  liminal:\n    url: http://gw\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    exchange_url: http://cabal\n"
        "  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"))
    assert isinstance(agent.tools, LiminalToolProvider)
    assert agent.tools.exchange_url == "http://cabal"
