"""ColleaguesToolProvider — a deployed agent finds and asks its switchboard-
listed colleagues over A2A. Unit tests against a fake httpx-shaped client,
mirroring liminal's injectable-client idiom (tests/test_tools_liminal.py)
and a2a_auth's injectable-fetch idiom for outbound HTTP."""

from __future__ import annotations

import pytest

from daedalus.tools.colleagues import ColleaguesToolProvider

NOT_CONFIGURED = "The colleague pass is not configured."


class FakeResponse:
    def __init__(self, status_code=200, payload=None, malformed=False):
        self.status_code = status_code
        self._payload = payload
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("not json")
        return self._payload


class FakeHTTPClient:
    """Records every call; serves canned responses in order per verb."""

    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, dict]] = []

    async def get(self, url, headers=None):
        self.get_calls.append((url, headers or {}))
        return self.get_responses.pop(0)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json or {}, headers or {}))
        return self.post_responses.pop(0)


DIRECTORY = {
    "agents": [
        {
            "name": "me",
            "team": "ops",
            "purpose_tags": ["self"],
            "capability": "irrelevant",
            "description": "irrelevant",
        },
        {
            "name": "rin",
            "team": "billing",
            "purpose_tags": ["refunds", "invoices"],
            "capability": "reads billing records",
            "description": "the billing specialist",
        },
    ]
}


def make_provider(client, *, token_env="COLLEAGUE_TOKEN", self_name="me"):
    return ColleaguesToolProvider(
        "http://switchboard.test", token_env=token_env, self_name=self_name, client=client
    )


# --- registration / config parse -------------------------------------------


def test_tools_spec_accepts_colleagues_provider_and_config(tmp_path):
    from daedalus.bundle import Bundle

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    spec = Bundle.load(tmp_path).spec.tools
    assert spec.provider == "colleagues"
    assert spec.colleagues.url == "http://switchboard.test"
    assert spec.colleagues.token_env == "CABAL_COLLEAGUE_TOKEN"
    assert spec.colleagues.self == "x"


def test_agent_wires_colleagues_provider_from_agent_yaml(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from daedalus.tools.colleagues import ColleaguesToolProvider
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM())
    assert isinstance(agent.tools, ColleaguesToolProvider)


def test_provider_colleagues_without_url_fails_at_load(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle, BundleError
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: colleagues\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    with pytest.raises(BundleError, match="tools.colleagues.url"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM())


async def test_tools_lists_both_tool_specs():
    provider = make_provider(FakeHTTPClient())
    specs = await provider.tools()
    assert [s["name"] for s in specs] == [
        "colleagues.list_colleagues",
        "colleagues.ask_colleague",
    ]


# --- list_colleagues happy path + self-exclusion ----------------------------


async def test_list_colleagues_excludes_self_and_renders_readable_lines(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    provider = make_provider(client)
    text = await provider.call("colleagues.list_colleagues", {})
    assert "me" not in text  # self excluded
    assert "rin" in text
    assert "billing" in text
    assert "refunds" in text and "invoices" in text
    assert "reads billing records" in text
    url, headers = client.get_calls[0]
    assert url == "http://switchboard.test/directory"
    assert headers["Authorization"] == "Bearer secret"


async def test_list_colleagues_empty_directory_says_so(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, {"agents": []})])
    provider = make_provider(client)
    assert await provider.call("colleagues.list_colleagues", {}) == (
        "No colleagues are listed yet."
    )


async def test_list_colleagues_only_self_says_so(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    only_self = {"agents": [DIRECTORY["agents"][0]]}
    client = FakeHTTPClient(get_responses=[FakeResponse(200, only_self)])
    provider = make_provider(client)
    assert await provider.call("colleagues.list_colleagues", {}) == (
        "No colleagues are listed yet."
    )


async def test_list_colleagues_http_error_names_the_status(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(500)])
    provider = make_provider(client)
    text = await provider.call("colleagues.list_colleagues", {})
    assert "500" in text
    assert "—" not in text and "--" not in text  # no em dashes anywhere


# --- ask_colleague happy path + envelope shape ------------------------------


async def test_ask_colleague_happy_path_returns_reply_text(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {
        "status": {"message": {"parts": [{"kind": "text", "text": "the invoice cleared"}]}}
    }
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "did it clear?"}
    )
    assert text == "the invoice cleared"


async def test_ask_colleague_envelope_shape_is_pinned(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {"status": {"message": {"parts": [{"kind": "text", "text": "ok"}]}}}
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "did it clear?"}
    )
    url, body, headers = client.post_calls[0]
    assert url == "http://switchboard.test/a2a/tasks"
    assert headers["Authorization"] == "Bearer secret"
    assert body["target"] == "rin"
    task = body["task"]
    assert task["id"].startswith("ask-")
    assert len(task["id"]) == len("ask-") + 16  # secrets.token_hex(8)
    assert task["kind"] == "task"
    assert task["status"]["state"] == "submitted"
    message = task["status"]["message"]
    assert message == {
        "kind": "message",
        "role": "user",
        "parts": [{"kind": "text", "text": "did it clear?"}],
    }
    assert task["history"] == []
    # full shape, id excluded, is exactly this — no extra keys smuggled in
    task_without_id = {k: v for k, v in task.items() if k != "id"}
    assert task_without_id == {
        "kind": "task",
        "status": {
            "state": "submitted",
            "message": {
                "kind": "message",
                "role": "user",
                "parts": [{"kind": "text", "text": "did it clear?"}],
            },
        },
        "history": [],
    }
    assert body == {"target": "rin", "task": task}


async def test_ask_colleague_multi_part_reply_joins_all_text_parts(monkeypatch):
    """The join-all-parts contract (transports.a2a.text_of, mirrored): a
    multi-part reply arrives whole, non-text parts are skipped, order kept.
    parts[0] alone would have silently dropped everything after the first."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {
        "status": {
            "message": {
                "parts": [
                    {"kind": "text", "text": "the invoice cleared"},
                    {"kind": "file", "uri": "s3://irrelevant"},
                    {"kind": "text", "text": "refund issued yesterday"},
                ]
            }
        }
    }
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "did it clear?"}
    )
    assert text == "the invoice cleared\nrefund issued yesterday"


async def test_ask_colleague_reply_with_no_text_parts_is_named_plainly(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {"status": {"message": {"parts": [{"kind": "file", "uri": "s3://x"}]}}}
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "hi"}
    )
    assert text == "The colleague answered in a shape I could not read."


async def test_ask_colleague_refuses_self_without_a_network_call(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient()
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "me", "question": "anything"}
    )
    assert "myself" in text.lower() or "cannot ask" in text.lower()
    assert client.post_calls == []
    assert "—" not in text


async def test_ask_colleague_http_error_names_the_status(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[FakeResponse(404)])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "unknown-agent", "question": "hi"}
    )
    assert "404" in text
    assert "—" not in text


async def test_ask_colleague_malformed_reply_is_named_plainly(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[FakeResponse(200, {"nothing": "useful"})])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "hi"}
    )
    assert text == "The colleague answered in a shape I could not read."


async def test_ask_colleague_malformed_json_is_named_plainly(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[FakeResponse(200, malformed=True)])
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "hi"}
    )
    assert text == "The colleague answered in a shape I could not read."


# --- token-absent soft degrade, both tools ----------------------------------


async def test_list_colleagues_degrades_when_token_env_unset():
    provider = ColleaguesToolProvider(
        "http://switchboard.test", token_env=None, self_name="me", client=FakeHTTPClient()
    )
    assert await provider.call("colleagues.list_colleagues", {}) == NOT_CONFIGURED


async def test_list_colleagues_degrades_when_token_env_value_empty(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "")
    client = FakeHTTPClient()
    provider = make_provider(client)
    assert await provider.call("colleagues.list_colleagues", {}) == NOT_CONFIGURED
    assert client.get_calls == []


async def test_ask_colleague_degrades_when_token_env_absent(monkeypatch):
    monkeypatch.delenv("COLLEAGUE_TOKEN", raising=False)
    client = FakeHTTPClient()
    provider = make_provider(client)
    text = await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "hi"}
    )
    assert text == NOT_CONFIGURED
    assert client.post_calls == []


async def test_boot_is_unaffected_by_a_missing_token(tmp_path):
    """Mirrors liminal's soft-degrade idiom: absent env is a polite call-time
    failure, never a boot failure."""
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: THIS_ENV_VAR_DOES_NOT_EXIST\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM())  # must not raise
    assert agent.tools is not None
