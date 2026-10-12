"""ColleaguesToolProvider — a deployed agent finds and asks its switchboard-
listed colleagues over A2A. Unit tests against a fake httpx-shaped client,
mirroring liminal's injectable-client idiom (tests/test_tools_liminal.py)
and a2a_auth's injectable-fetch idiom for outbound HTTP."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from daedalus.tools.colleagues import (
    COLLEAGUE_DOWN_FOR_S,
    COLLEAGUE_STALE_AFTER_S,
    ColleaguesToolProvider,
    consulted_colleagues,
)

NOT_CONFIGURED = "The colleague pass is not configured."


def _techne_installed() -> bool:
    # techne is not on PyPI; local runs use the sibling checkout (PYTHONPATH=src:../techne).
    return importlib.util.find_spec("techne") is not None


class FakeResponse:
    def __init__(self, status_code=200, payload=None, malformed=False, text=""):
        self.status_code = status_code
        self._payload = payload
        self._malformed = malformed
        self.text = text

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


def make_provider(client, *, token_env="COLLEAGUE_TOKEN", self_name="me", db_path=None):
    return ColleaguesToolProvider(
        "http://switchboard.test",
        token_env=token_env,
        self_name=self_name,
        client=client,
        db_path=db_path,
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


async def test_tools_lists_all_three_tool_specs():
    provider = make_provider(FakeHTTPClient())
    specs = await provider.tools()
    assert [s["name"] for s in specs] == [
        "colleagues.list_colleagues",
        "colleagues.ask_colleague",
        "colleagues.remember",
    ]


# --- fetch_agents: public, token-gated --------------------------------------
#
# fetch_agents() used to be the private _fetch_agents(), fired the directory
# GET unconditionally (unlike its siblings _list_colleagues/_remember, which
# check the token first), and was reached across the module boundary by
# revision.py's ColleaguesLike Protocol -- a private-method leak. It is now
# public, and gates on the token itself before ever touching the network.


async def test_fetch_agents_is_public_and_returns_the_directory(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    provider = make_provider(client)
    agents, error = await provider.fetch_agents()
    assert error is None
    assert agents == DIRECTORY["agents"]
    url, headers = client.get_calls[0]
    assert url == "http://switchboard.test/directory"
    assert headers["Authorization"] == "Bearer secret"


async def test_fetch_agents_raises_typed_error_without_a_token_env_and_never_touches_network():
    from daedalus.tools.colleagues import ColleaguesNotConfigured

    class _NeverCalledClient:
        async def get(self, url, headers=None):
            raise AssertionError("fetch_agents must token-gate before any network call")

    provider = ColleaguesToolProvider(
        "http://switchboard.test", token_env=None, self_name="me", client=_NeverCalledClient()
    )
    with pytest.raises(ColleaguesNotConfigured):
        await provider.fetch_agents()


async def test_fetch_agents_raises_typed_error_when_token_env_value_is_empty(monkeypatch):
    from daedalus.tools.colleagues import ColleaguesNotConfigured

    monkeypatch.setenv("COLLEAGUE_TOKEN", "")
    client = FakeHTTPClient()
    provider = make_provider(client)
    with pytest.raises(ColleaguesNotConfigured):
        await provider.fetch_agents()
    assert client.get_calls == []


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
    # full shape, id excluded, is exactly this — no extra keys smuggled in.
    #
    # `metadata` joined the pinned shape with cabal#144: the hop count has to
    # cross the A2A boundary or the cross-agent cycle is unbounded. This
    # assertion is the reason that change could not land quietly, which is
    # what an exact-shape pin is for — so it is UPDATED here, not loosened.
    task_without_id = {k: v for k, v in task.items() if k != "id"}
    assert task_without_id == {
        "kind": "task",
        "metadata": {"cabal_delegation_depth": 1},
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


async def test_ask_colleague_propagates_the_inbound_context_id(monkeypatch):
    """The switchboard's loop guard groups hops by contextId. Without this,
    every hop mints a fresh id and the guard is inert -- which is how
    gemini-one and probe looped five times in issue #5."""
    from daedalus.tools.colleagues import current_context_id

    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {"status": {"message": {"parts": [{"kind": "text", "text": "ok"}]}}}
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    token = current_context_id.set("ctx-from-inbound")
    try:
        await provider.call(
            "colleagues.ask_colleague", {"name": "rin", "question": "did it clear?"}
        )
    finally:
        current_context_id.reset(token)
    _, body, _ = client.post_calls[0]
    assert body["task"]["contextId"] == "ctx-from-inbound"


async def test_ask_colleague_envelope_omits_context_id_when_none_is_set(monkeypatch):
    """Regression guard: with no context threaded, the envelope stays exactly
    as it was before this feature -- no contextId key at all."""
    from daedalus.tools.colleagues import current_context_id

    assert current_context_id.get() is None
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    reply = {"status": {"message": {"parts": [{"kind": "text", "text": "ok"}]}}}
    client = FakeHTTPClient(post_responses=[FakeResponse(200, reply)])
    provider = make_provider(client)
    await provider.call(
        "colleagues.ask_colleague", {"name": "rin", "question": "did it clear?"}
    )
    _, body, _ = client.post_calls[0]
    assert "contextId" not in body["task"]


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


# --- remember happy path + provenance shapes --------------------------------


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_remember_stores_fact_with_team_provenance_and_low_confidence(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    db_path = tmp_path / "techne.db"
    provider = make_provider(client, db_path=db_path)
    with patch("techne.core.knowledge.store") as fake_store:
        text = await provider.call(
            "colleagues.remember",
            {"fact": "Refunds take 3 business days.", "learned_from": "rin"},
        )
    fake_store.assert_called_once_with(
        db_path,
        "Refunds take 3 business days.",
        type="fact",
        confidence="low",
        source="colleague:rin#team=billing",
    )
    assert "rin" in text
    assert "—" not in text


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_remember_stores_fact_without_team_suffix_when_team_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    directory = {
        "agents": [
            {"name": "me", "team": "ops"},
            {"name": "zeb", "team": ""},
        ]
    }
    client = FakeHTTPClient(get_responses=[FakeResponse(200, directory)])
    db_path = tmp_path / "techne.db"
    provider = make_provider(client, db_path=db_path)
    with patch("techne.core.knowledge.store") as fake_store:
        text = await provider.call(
            "colleagues.remember", {"fact": "The build is green.", "learned_from": "zeb"}
        )
    fake_store.assert_called_once_with(
        db_path, "The build is green.", type="fact", confidence="low", source="colleague:zeb"
    )
    assert "zeb" in text


# --- remember refusals -------------------------------------------------------


async def test_remember_refuses_self_without_a_network_call(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient()
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    text = await provider.call(
        "colleagues.remember", {"fact": "anything", "learned_from": "me"}
    )
    assert "myself" in text.lower() or "cannot" in text.lower()
    assert client.get_calls == []
    assert "—" not in text


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_remember_refuses_a_name_absent_from_the_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    with patch("techne.core.knowledge.store") as fake_store:
        text = await provider.call(
            "colleagues.remember", {"fact": "anything", "learned_from": "ghost"}
        )
    assert "ghost" in text
    fake_store.assert_not_called()
    assert "—" not in text


# --- remember failure modes (never raise) -----------------------------------


async def test_remember_directory_fetch_failure_is_named_plainly(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(500)])
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    text = await provider.call(
        "colleagues.remember", {"fact": "anything", "learned_from": "rin"}
    )
    assert "500" in text
    assert "—" not in text


async def test_remember_degrades_when_db_path_is_absent(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient()
    provider = make_provider(client, db_path=None)
    text = await provider.call(
        "colleagues.remember", {"fact": "anything", "learned_from": "rin"}
    )
    assert client.get_calls == []  # no point fetching the directory; nowhere to store it
    assert "—" not in text


async def test_remember_degrades_when_token_env_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("COLLEAGUE_TOKEN", raising=False)
    client = FakeHTTPClient()
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    text = await provider.call(
        "colleagues.remember", {"fact": "anything", "learned_from": "rin"}
    )
    assert text == NOT_CONFIGURED
    assert client.get_calls == []


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_remember_store_failure_is_named_plainly(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    with patch("techne.core.knowledge.store", side_effect=RuntimeError("disk full")):
        text = await provider.call(
            "colleagues.remember", {"fact": "anything", "learned_from": "rin"}
        )
    assert "disk full" in text or "could not" in text.lower()
    assert "—" not in text


# --- the directory envelope stays exactly the same for remember too --------


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
async def test_remember_directory_request_shape_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(get_responses=[FakeResponse(200, DIRECTORY)])
    provider = make_provider(client, db_path=tmp_path / "techne.db")
    with patch("techne.core.knowledge.store"):
        await provider.call(
            "colleagues.remember", {"fact": "anything", "learned_from": "rin"}
        )
    url, headers = client.get_calls[0]
    assert url == "http://switchboard.test/directory"
    assert headers["Authorization"] == "Bearer secret"


# --- db_path wiring at the Agent level --------------------------------------


@pytest.mark.skipif(not _techne_installed(), reason="techne not installed")
def test_agent_threads_techne_db_path_into_colleagues_provider(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        f"memory:\n  personal: techne\n  home: {tmp_path}\n"
        "tools:\n  provider: colleagues\n  colleagues:\n"
        "    url: http://switchboard.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n"
        "    self: x\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM())
    assert isinstance(agent.tools, ColleaguesToolProvider)
    assert agent.tools.db_path == agent.memory.db_path
    assert agent.tools.db_path is not None


def test_agent_leaves_colleagues_db_path_none_without_techne_memory(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
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
    assert agent.tools.db_path is None


# ---- daedalus#38: the directory must not offer unreachable colleagues -------
#
# From the production incident of 2026-09-10: two agents spent ~270 and ~188
# calls asking questions of `sherlock-lite` (scaled 0/0), `thegeneral-hague`
# (0/0) and `echo` (no deployment at all). "Could not reach X" was permanent
# and unconditional, and nothing in the tool surface said so.


def _live_dir(*agents):
    return {"agents": list(agents)}


def _agent(name, *, heartbeat_age_s=None, status="active"):
    """A directory row. `heartbeat_age_s=None` means the agent has NEVER
    reported in -- the `echo`-with-no-deployment shape."""
    row = {
        "name": name,
        "team": "ops",
        "purpose_tags": ["t"],
        "capability": "c",
        "status": status,
    }
    if heartbeat_age_s is not None:
        beat = datetime.now(UTC) - timedelta(seconds=heartbeat_age_s)
        row["last_heartbeat"] = beat.isoformat()
    return row


@pytest.mark.asyncio
async def test_a_colleague_with_a_stale_heartbeat_is_not_offered(monkeypatch):
    """THE sherlock-lite CASE. status stays 'active' when a pod scales to 0/0;
    only the heartbeat going stale reveals it, which is why `status` alone
    would not have caught the incident."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[
            FakeResponse(payload=_live_dir(
                _agent("rin", heartbeat_age_s=5),
                _agent("sherlock-lite", heartbeat_age_s=3600),
            ))
        ]
    )
    out = await make_provider(client)._list_colleagues()
    assert "rin" in out
    assert "sherlock-lite" not in out


@pytest.mark.asyncio
async def test_a_colleague_that_never_reported_in_is_not_offered(monkeypatch):
    """THE echo CASE -- listed, never deployed, so no heartbeat ever."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[
            FakeResponse(payload=_live_dir(
                _agent("rin", heartbeat_age_s=5),
                _agent("echo"),
            ))
        ]
    )
    out = await make_provider(client)._list_colleagues()
    assert "rin" in out
    assert "echo" not in out


@pytest.mark.asyncio
async def test_a_directory_without_liveness_is_not_filtered(monkeypatch):
    """THE SAFETY PROPERTY, and the reason the filter is conditional.

    "No agent carries a heartbeat" means the switchboard does not publish
    liveness -- filtering then would empty the directory and break every
    deployment whose switchboard predates the field. Degrade to the old
    behaviour instead. Distinct from "THIS agent has no heartbeat", which is
    the echo case above and DOES hide it.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(get_responses=[FakeResponse(payload=DIRECTORY)])
    out = await make_provider(client)._list_colleagues()
    assert "rin" in out, "a liveness-less directory must still list its colleagues"


@pytest.mark.asyncio
async def test_the_model_is_told_how_fresh_a_colleague_is(monkeypatch):
    """Surfaced, not merely filtered. A model shown only reachable colleagues
    cannot tell 'nobody can answer' from 'I have not looked hard enough', and
    the incident is what the second belief costs."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[FakeResponse(payload=_live_dir(_agent("rin", heartbeat_age_s=7)))]
    )
    out = await make_provider(client)._list_colleagues()
    # THE VALUE, NOT THE LABEL (#38 r1 Nit 5). This asserted only that the
    # substring was present, so changing the format string to `int(age) + 999`
    # left all 40 tests green -- the assertion ran and could not see the number,
    # which is the line's entire purpose.
    assert "last seen: 7s ago" in out, out


@pytest.mark.asyncio
async def test_everything_hidden_says_so_rather_than_looking_empty(monkeypatch):
    """An agent told 'no colleagues are listed' while three unreachable ones
    sit behind the filter has been handed the same dead end in nicer words."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[
            FakeResponse(payload=_live_dir(
                _agent("sherlock-lite", heartbeat_age_s=3600),
                _agent("echo"),
            ))
        ]
    )
    out = await make_provider(client)._list_colleagues()
    assert "not responding" in out
    assert "will not change by retrying" in out
    assert "No colleagues are listed yet." not in out


# ---- the runtime half: remember that a colleague is down -------------------


@pytest.mark.asyncio
async def test_repeated_failures_mark_a_colleague_down_and_stop_the_asking(monkeypatch):
    """THE CORE LESSON OF THE INCIDENT. A terminal error closes one affordance;
    it does not terminate the task. After N consecutive failures the answer
    must say the door is shut, not hand back another transient-looking error
    the agent will read as a reason to try again."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(post_responses=[FakeResponse(status_code=503)] * 2)
    provider = make_provider(client)

    first = await provider._ask_colleague("ghost", "q")
    second = await provider._ask_colleague("ghost", "q")
    third = await provider._ask_colleague("ghost", "q")

    assert "503" in first and "503" in second
    assert "stopped trying" in third
    assert "retrying it will fail the same way" in third
    assert len(client.post_calls) == 2, (
        "the third ask must not reach the network at all; that is the point"
    )


@pytest.mark.asyncio
async def test_a_success_clears_the_failure_record(monkeypatch):
    """CONSECUTIVE failures are what matter. A colleague that answers between
    two blips is not failing, and treating it as such would ban a healthy
    peer after enough unrelated flakes."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hello"}]}}
    })
    client = FakeHTTPClient(
        post_responses=[
            FakeResponse(status_code=503),   # failure 1
            good,                            # success -- must clear the count
            FakeResponse(status_code=503),   # failure 1 again, if it cleared
            good,                            # reached ONLY if it cleared
        ]
    )
    provider = make_provider(client)
    await provider._ask_colleague("rin", "q")
    assert "hello" in await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")

    # THE FOURTH ASK IS WHERE THE DIFFERENCE SHOWS, and an earlier version of
    # this test stopped at the third and therefore proved nothing: the
    # down-check runs at the START of a call, so the third ask returns its own
    # 503 whether or not the success cleared anything. Caught by mutation --
    # blanking _record_success left the whole file green.
    out = await provider._ask_colleague("rin", "q")
    assert "stopped trying" not in out, (
        "a success between two failures must reset the count; without that, "
        "two unrelated blips either side of a healthy exchange ban a live peer"
    )
    assert "hello" in out


@pytest.mark.asyncio
async def test_a_downed_colleague_is_dropped_from_the_directory(monkeypatch):
    """The directory filter catches an agent that was already down when the
    directory was read; this catches one that died after."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[FakeResponse(payload=_live_dir(_agent("rin", heartbeat_age_s=5)))],
        post_responses=[FakeResponse(status_code=503)] * 2,
    )
    provider = make_provider(client)
    await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")
    out = await provider._list_colleagues()
    assert "rin" not in out
    assert "not responding" in out


@pytest.mark.asyncio
async def test_a_partial_hide_is_counted_rather_than_swallowed(monkeypatch):
    """daedalus#38 pre-pass r3 — THE CASE THE MITIGATION DID NOT COVER.

    The "N are listed but not responding" sentence fired only when EVERY
    colleague was filtered. A directory with one reachable colleague and three
    hidden ones printed the one and said nothing about the rest — the same
    ambiguity the total-blackout branch exists to remove, in a smaller room.

    That matters beyond tidiness, because it is the argument the liveness
    filter rests on. The filter trusts a `last_heartbeat` field whose source is
    unidentified, and the reason that is acceptable is that a wrongly-hidden
    colleague surfaces as a count rather than as silence. That was true only of
    the case it was written to generalize.

    Asserts the hidden ones are NOT NAMED as well as counted: naming a
    colleague the tool has just judged unreachable invites the ask it exists to
    prevent.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[
            FakeResponse(payload=_live_dir(
                _agent("rin", heartbeat_age_s=5),
                _agent("sherlock-lite", heartbeat_age_s=3600),
                _agent("thegeneral-hague", heartbeat_age_s=3600),
                _agent("echo"),
            ))
        ]
    )
    out = await make_provider(client)._list_colleagues()

    assert "rin" in out
    assert "3 further colleagues are listed but not responding" in out, out
    for hidden in ("sherlock-lite", "thegeneral-hague", "echo"):
        assert hidden not in out, hidden


@pytest.mark.asyncio
async def test_nothing_hidden_adds_no_count_line(monkeypatch):
    """The count is absent, not zero — an all-reachable directory must not
    carry a parenthetical about colleagues that do not exist."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        get_responses=[
            FakeResponse(payload=_live_dir(
                _agent("rin", heartbeat_age_s=5),
                _agent("shaz", heartbeat_age_s=7),
            ))
        ]
    )
    out = await make_provider(client)._list_colleagues()

    assert "rin" in out and "shaz" in out
    assert "not responding" not in out, out


@pytest.mark.asyncio
async def test_an_unreadable_heartbeat_does_not_black_out_the_fleet(monkeypatch):
    """daedalus#38 r1 Important 1 — THE FOURTH SHAPE.

    `_directory_publishes_liveness` tested for the KEY while its docstring
    promised a PROOF. Those differ on a value that does not parse: the module
    reads it as "never reported in", which is the echo-with-no-deployment case,
    so a whole healthy fleet was hidden at once — under the total-blackout
    sentence that tells the model retrying will not help.

    Epoch seconds is the dialect here because it is the commonest way to
    publish a heartbeat and `datetime.fromisoformat(str(1757600000))` raises.
    The field being MISSING is the colleague's fact; the field being
    UNREADABLE is ours, and a colleague should not be charged for it.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    rows = []
    for name in ("rin", "shaz", "gemini-one"):
        row = _agent(name)
        row["last_heartbeat"] = 1757600000  # epoch seconds, not ISO-8601
        rows.append(row)
    client = FakeHTTPClient(get_responses=[FakeResponse(payload=_live_dir(*rows))])

    out = await make_provider(client)._list_colleagues()

    assert "not responding" not in out, out
    for name in ("rin", "shaz", "gemini-one"):
        assert name in out, f"{name} was hidden for a heartbeat WE could not read"


@pytest.mark.asyncio
async def test_a_real_answer_between_two_blips_clears_the_record(monkeypatch):
    """daedalus#38 r1 Important 2 — "CONSECUTIVE" WAS NOT THE PREDICATE.

    `_record_success` ran after the JSON parse and the empty-text check, so a
    colleague answering a perfectly good 200 with no text part cleared nothing.
    Blip, real round trip, blip banned it — the docstring's own counterexample.

    The counter counts TRANSPORT events, so the clear has to as well. This
    walks the exact four asks that produced the false ban.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    empty = FakeResponse(payload={"status": {"message": {"parts": []}}})
    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
    })
    client = FakeHTTPClient(post_responses=[
        FakeResponse(status_code=503),
        empty,
        FakeResponse(status_code=503),
        good,
    ])
    provider = make_provider(client)

    await provider._ask_colleague("rin", "q")   # blip
    await provider._ask_colleague("rin", "q")   # a real round trip
    await provider._ask_colleague("rin", "q")   # blip
    fourth = await provider._ask_colleague("rin", "q")

    assert "stopped trying" not in fourth, fourth
    assert fourth == "hi", "the fourth ask never reached the network"


@pytest.mark.asyncio
async def test_two_failures_a_day_apart_do_not_ban(monkeypatch):
    """daedalus#38 r1 Important 2, second half — THE COUNT HAD NO DECAY.

    A bare-count record carried no timestamp and was evicted by nothing but a
    successful ask, so `_is_marked_down` never reached it and a single failure
    was immortal. Two failures a day apart therefore banned a healthy
    colleague — and a colleague the agent asks rarely is exactly the one that
    accumulates.

    The constant's comment justifies two-not-one because "a single failure is
    as likely to be a blip as a death". Across time the code gave none of that
    protection; this is that sentence made true.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    clock = {"t": 1000.0}
    monkeypatch.setattr("daedalus.tools.colleagues.time.time", lambda: clock["t"])
    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
    })
    client = FakeHTTPClient(post_responses=[
        FakeResponse(status_code=503), FakeResponse(status_code=503), good,
    ])
    provider = make_provider(client)

    await provider._ask_colleague("rin", "q")
    clock["t"] += 86400.0
    await provider._ask_colleague("rin", "q")
    clock["t"] += 1.0
    third = await provider._ask_colleague("rin", "q")

    assert "stopped trying" not in third, third
    assert third == "hi"
    assert provider._failures.get("rin") is None, (
        "a forgotten blip was still on the record"
    )


@pytest.mark.asyncio
async def test_the_ban_lasts_its_full_window_and_then_forgets(monkeypatch):
    """daedalus#38 r1 Nit 3 — THE BREAKER'S TIME DIMENSION WAS UNTESTED.

    No test advanced a clock, so two of the four documented properties survived
    mutation: `COLLEAGUE_DOWN_FOR_S = 0.001` left all 40 green because nothing
    asserted the ban outlives the test's own wall clock, and replacing the
    TTL-expiry `pop` with `pass` left all 40 green too. This is the fixture
    that would have caught both r1 Importants.

    Walks one dead colleague across the boundary: banned inside the window,
    retried after it, and the record forgotten rather than decremented — so the
    retry is a genuine one instead of being a single failure from a new ban.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    clock = {"t": 1000.0}
    monkeypatch.setattr("daedalus.tools.colleagues.time.time", lambda: clock["t"])
    client = FakeHTTPClient(post_responses=[
        FakeResponse(status_code=503), FakeResponse(status_code=503),
        FakeResponse(status_code=503),
    ])
    provider = make_provider(client)

    await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")

    clock["t"] += COLLEAGUE_DOWN_FOR_S - 1
    inside = await provider._ask_colleague("rin", "q")
    assert "stopped trying" in inside, "the ban did not last its window"
    assert len(client.post_calls) == 2, "a banned ask still reached the network"

    clock["t"] += 2
    await provider._ask_colleague("rin", "q")
    assert len(client.post_calls) == 3, "the ban never expired"
    assert provider._failures.get("rin", [0.0])[0] == 1, (
        "the expired verdict was decremented rather than forgotten, so the "
        "retry was one failure from a new ban"
    )


def test_the_breaker_constants_are_pinned() -> None:
    """Both numbers were unpinned, and the ban window was WORSE than unpinned:
    the walk test below expresses its clock steps as `COLLEAGUE_DOWN_FOR_S - 1`,
    so shrinking the constant shrinks the test with it and 0.001 stayed green.
    A relative assertion measures the mechanism and cannot see the value.

    120s is a chosen staleness threshold rather than a derived one — whatever
    serves /directory is not cabal, so cabal's 30s beat is the only interval
    anyone has measured rather than the one that applies. A real interval above
    it reads every colleague as permanently stale and hides the fleet.

    300s is the ban window, and it is also the forget window: one rule.
    """
    assert COLLEAGUE_STALE_AFTER_S == 120.0
    assert COLLEAGUE_DOWN_FOR_S == 300.0


@pytest.mark.asyncio
async def test_the_ban_is_stamped_after_the_request_not_before(monkeypatch):
    """daedalus#38 r1 Nit 6 — A SLOW FAILURE USED TO BUY A SHORTER BAN.

    `now` was captured before the POST and `down_until` stamped from it, so a
    request that hung to its 180s timeout bought a 120s ban rather than 300.
    That matters more than it looks: a pod scaled to 0/0 behind a proxy fails
    BY timeout, which is the incident's own shape and the case where the ban
    shrank most.

    The clock advances inside the transport here, which is the only way to see
    it — with an instant fake client the two timestamps are identical.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    clock = {"t": 1000.0}
    monkeypatch.setattr("daedalus.tools.colleagues.time.time", lambda: clock["t"])

    class SlowFailingClient(FakeHTTPClient):
        async def post(self, url, json=None, headers=None, timeout=None):
            clock["t"] += 180.0
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    client = SlowFailingClient(post_responses=[
        FakeResponse(status_code=503), FakeResponse(status_code=503),
    ])
    provider = make_provider(client)

    await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")

    down_until = provider._failures["rin"][1]
    assert down_until == clock["t"] + COLLEAGUE_DOWN_FOR_S, (
        f"the ban was stamped {clock['t'] + COLLEAGUE_DOWN_FOR_S - down_until}s "
        "before the request finished, so it is that much shorter"
    )


def test_the_staleness_threshold_is_two_minutes() -> None:
    """daedalus#38 r1 Nit 4 — the number was unpinned; a tenfold change left
    the suite green, so any value between roughly 8s and 3599s passed.

    It is a CHOSEN threshold, not a derived one: whatever serves /directory is
    not cabal, so cabal's 30s beat is the only interval anyone has measured
    rather than the one that applies. Pinned so that changing it is a decision
    — and the decision matters, because a real interval above this value reads
    every colleague as permanently stale and hides the fleet.
    """
    assert COLLEAGUE_STALE_AFTER_S == 120.0


@pytest.mark.asyncio
async def test_a_slow_failure_does_not_complete_a_forgotten_blip(monkeypatch):
    """daedalus#38 r2 Important — THE MOTION THE OTHER CLOCK TESTS DO NOT MAKE.

    `_ask_colleague` reads the clock BEFORE the POST and stamps the failure with
    a fresh one AFTER it, so a hung request opens a gap between the guard's view
    of the record and the increment's. The record is "absent or recent" as of
    the guard, not as of the count.

    Two failures 380s apart — beyond the 300s forget window — therefore
    completed a ban instead of restarting the count. Bounded at
    COLLEAGUE_DOWN_FOR_S + timeout rather than at COLLEAGUE_DOWN_FOR_S, and it
    bites hardest in the timeout case, which is the shape a pod scaled to 0/0
    behind a proxy produces.

    THIS IS WHY REMOVING THE `_record_failure` RESET AS "REDUNDANT" SURVIVED ITS
    MUTANT. The other time-aware tests advance the clock BETWEEN asks; none
    advanced it WITHIN one, so nothing could see the gap. The redundancy was
    structural and the count needs temporal.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    clock = {"t": 1000.0}
    monkeypatch.setattr("daedalus.tools.colleagues.time.time", lambda: clock["t"])

    class HangingClient(FakeHTTPClient):
        """Advances the clock during the request, the way a timeout does."""

        def __init__(self, *a, hang_on=(), **kw):
            super().__init__(*a, **kw)
            self._hang_on = set(hang_on)
            self._n = 0

        async def post(self, url, json=None, headers=None, timeout=None):
            self._n += 1
            if self._n in self._hang_on:
                clock["t"] += 180.0
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
    })
    client = HangingClient(
        post_responses=[FakeResponse(status_code=503), FakeResponse(status_code=503), good],
        hang_on={2},
    )
    provider = make_provider(client)

    await provider._ask_colleague("rin", "q")   # instant 503 at t=1000
    clock["t"] += 200.0                          # 200s later: inside the window
    await provider._ask_colleague("rin", "q")   # hangs 180s, so it lands at t=1380

    gap = 380.0
    assert gap > COLLEAGUE_DOWN_FOR_S, "the fixture no longer spans the window"

    third = await provider._ask_colleague("rin", "q")
    assert "stopped trying" not in third, (
        f"two failures {gap}s apart completed a ban under a "
        f"{COLLEAGUE_DOWN_FOR_S}s forget window: {third}"
    )
    assert third == "hi"


# ---- cabal#144: depth must survive the A2A hop ----------------------------


@pytest.mark.asyncio
async def test_an_ask_carries_the_hop_count_outbound(monkeypatch):
    """WITHOUT THIS THE CROSS-AGENT CYCLE IS UNBOUNDED.

    cabal bounds delegation depth in its router, but a colleague call carrying
    no depth arrives at 0 however deep the caller already was. So
    `ledger -> gemini-one -> ledger` -- the shape from the 2026-09-10 incident
    -- resets on every hop and the bound never fires.
    """
    from daedalus.tools.colleagues import DEPTH_KEY, current_delegation_depth

    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
    })
    client = FakeHTTPClient(post_responses=[good])
    current_delegation_depth.set(3)
    try:
        await make_provider(client)._ask_colleague("rin", "q")
    finally:
        current_delegation_depth.set(0)

    _, body, _ = client.post_calls[0]
    assert body["task"]["metadata"][DEPTH_KEY] == 4, (
        "the ask itself is a hop, so it must be sent at parent+1"
    )




@pytest.mark.asyncio
async def test_depth_is_sent_even_at_the_top_of_a_chain(monkeypatch):
    """The key is always present, and the minimum ever sent is 1.

    NOT because the receiver tells zero from absent — it does not, and this
    docstring said otherwise while the commit containing it withdrew that claim
    in two other files (#40 r1 Nit). cabal reads `metadata.get(DEPTH_KEY, 0)`,
    so both collapse onto the same value through the same path.

    The surviving reason is narrower and is what this pins: a receiver can only
    ever draw that distinction if the sender is unconditional about it, and we
    never send 0 anyway, so being explicit costs nothing and fixes one end of
    the contract."""
    from daedalus.tools.colleagues import DEPTH_KEY

    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    good = FakeResponse(payload={
        "status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}
    })
    client = FakeHTTPClient(post_responses=[good])
    await make_provider(client)._ask_colleague("rin", "q")
    _, body, _ = client.post_calls[0]
    assert body["task"]["metadata"][DEPTH_KEY] == 1




@pytest.mark.asyncio
async def test_an_inbound_depth_reaches_the_outbound_envelope(monkeypatch):
    """daedalus#40 r1 Important — THE SEAM, AND THE ONE LINE THE FEATURE RESTS ON.

    `Agent.handle_task` doing `current_delegation_depth.set(_inbound_depth)` is
    the sole join between the parser and the sender, and nothing crossed it.
    Replacing it with `set(0)` — every inbound depth discarded, every outbound
    ask stamped 1 however deep the chain already is, which is exactly the
    `ledger -> gemini-one -> ledger` cycle this PR exists to close — left the
    whole suite green.

    THE TWO NEW TEST GROUPS EACH SIT ON ONE SIDE OF IT. The agent-side test
    sets metadata and reads the contextvar, so under that mutant it reads 0 and
    passes MORE easily; the colleague-side tests set the contextvar by hand and
    never go through `handle_task`. The assertions ran and could not see.

    Worth a test rather than a note because the regression is SILENT. A broken
    contextId at least perturbs the switchboard's loop grouping; a broken depth
    produces no error anywhere — the bound simply stops firing, and the chain
    that returns through a remote peer is unbounded again.

    Goes through a REAL `handle_task`, in the shape
    test_agent.py::test_consulted_colleague_omitted_from_draft_gets_appended
    already uses for `consulted_colleagues`.
    """
    from daedalus.agent import Agent
    from daedalus.task import Task
    from daedalus.tools.colleagues import DEPTH_KEY
    from tests.fakes import ScriptedLLM

    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[
            FakeResponse(
                payload={
                    "status": {
                        "message": {"parts": [{"kind": "text", "text": "hi"}]}
                    }
                }
            )
        ]
    )
    provider = make_provider(client)

    class AskingLLM(ScriptedLLM):
        async def complete(self, messages, tools=None):
            await provider._ask_colleague("rin", "q")
            return await super().complete(messages, tools)

    agent = Agent.load(
        Path(__file__).parent / "golden" / "greeter", llm=AskingLLM("done")
    )
    task = Task.from_text("hi")
    task.metadata[DEPTH_KEY] = 3
    await agent.handle_task(task)

    _, body, _ = client.post_calls[0]
    assert body["task"]["metadata"][DEPTH_KEY] == 4, (
        "the inbound depth did not reach the outbound ask"
    )




@pytest.mark.asyncio
async def test_a_refused_ask_is_not_credited_as_a_consultation(monkeypatch):
    """daedalus#40 r2 Important — A REFUSAL ARRIVES AS HTTP 200.

    cabal turns a guardrail BLOCK into an annotated Task — `state="failed"`,
    the refusal as the single agent text part — and returns it through a
    `response_model=Task` route. Reading only `status.message` turned that
    refusal into an answer AND appended the target to the consultation ledger,
    so the agent told its caller it had consulted a colleague who never saw the
    question. The attribution law fires deterministically after post-draft
    enforcement, so that credit cannot be stripped downstream.

    THE READER GAP PRE-DATES THIS BRANCH — the doom-loop and budget guardrails
    produce the identical shape. What this branch owns is the door: before it,
    daedalus sent no depth key, cabal read the default 0, and the depth
    guardrail could never refuse a daedalus-originated ask.

    Uses cabal's real refusal shape rather than an invented one.
    """
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    refusal = FakeResponse(payload={
        "status": {
            "state": "failed",
            "message": {"parts": [{
                "kind": "text",
                "text": (
                    "router guardrail blocked: Delegation depth 8 has reached "
                    "the limit of 8."
                ),
            }]},
        },
        "metadata": {"cabal_reason_code": "POLICY_VIOLATION"},
    })
    client = FakeHTTPClient(post_responses=[refusal])
    ledger: list[str] = []
    consulted_colleagues.set(ledger)

    out = await make_provider(client)._ask_colleague("rin", "q")

    assert ledger == [], "a colleague that refused the ask was credited with answering it"
    assert out.startswith("rin did not answer:"), out
    assert "Delegation depth 8" in out, "the refusal's own explanation was dropped"




@pytest.mark.asyncio
async def test_an_answered_ask_is_still_credited(monkeypatch):
    """The control. A `completed` reply must still reach the ledger — a state
    check that rejects everything would pass the test above and break the
    feature it guards."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    good = FakeResponse(payload={
        "status": {
            "state": "completed",
            "message": {"parts": [{"kind": "text", "text": "the PSA range is X"}]},
        },
    })
    client = FakeHTTPClient(post_responses=[good])
    ledger: list[str] = []
    consulted_colleagues.set(ledger)

    out = await make_provider(client)._ask_colleague("rin", "q")

    assert ledger == ["rin"]
    assert out == "the PSA range is X"


# ---- #149: per-answer limits on ask_colleague ---------------------------------
#
# Observed 2026-10-06: an agent asked about a system no colleague had a tool for
# asked one colleague three times in one answer, kept going after "I don't have
# access", and the question bounced back to it. Every "I can't" was an HTTP 200,
# so the breaker counted it a success.

CANT = (
    "I don't have direct access to execute shell commands or call external APIs. "
    "Here's what you need to run in your terminal..."
)


def _answer(text):
    return FakeResponse(200, {"status": {"message": {"parts": [{"kind": "text", "text": text}]}}})


async def _ask(provider, name, question="what is monitor 42?"):
    return await provider.call("colleagues.ask_colleague", {"name": name, "question": question})


def _asked(client):
    return [body["target"] for _, body, _ in client.post_calls]


@pytest.fixture
def ledger():
    from daedalus.tools.colleagues import AskLedger, ask_ledger

    token = ask_ledger.set(AskLedger())
    yield ask_ledger.get()
    ask_ledger.reset(token)


async def test_a_fourth_ask_in_one_answer_is_refused_without_dispatching(monkeypatch, ledger):
    from daedalus.tools.colleagues import MAX_ASKS_PER_ANSWER

    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[_answer("ok")] * 3)
    provider = make_provider(client)
    for name in ("rin", "ana", "bo"):
        await _ask(provider, name)
    out = await _ask(provider, "cy")
    assert MAX_ASKS_PER_ANSWER == 3
    assert _asked(client) == ["rin", "ana", "bo"]
    assert "limit of 3" in out


async def test_a_repeat_ask_returns_the_earlier_reply_and_does_not_count(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[_answer("p99 latency over 2s")] + [_answer("ok")] * 2)
    provider = make_provider(client)
    await _ask(provider, "rin")
    again = await _ask(provider, "rin", "same thing, reworded")
    await _ask(provider, "ana")
    await _ask(provider, "bo")
    assert _asked(client) == ["rin", "ana", "bo"]
    assert "already asked rin" in again and "p99 latency over 2s" in again


async def test_cant_help_ends_the_hunt_for_every_colleague(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[_answer(CANT)])
    provider = make_provider(client)
    first = await _ask(provider, "rin")
    after = await _ask(provider, "ana")
    assert first.startswith(CANT) and "rin can't help" in first
    assert _asked(client) == ["rin"]
    assert "colleague asks are off" in after


async def test_an_ambiguous_reply_is_an_answer_and_the_hunt_goes_on(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    hedged = "It tracks p99 latency. I can't be sure of the threshold, but it fired at 19:27."
    client = FakeHTTPClient(post_responses=[_answer(hedged), _answer("ok")])
    provider = make_provider(client)
    assert await _ask(provider, "rin") == hedged
    await _ask(provider, "ana")
    assert _asked(client) == ["rin", "ana"]


def test_cant_help_matches_narrowly_and_only_near_the_start():
    from daedalus.tools.colleagues import is_cant_help

    assert is_cant_help(CANT)
    assert is_cant_help("I'm unable to reach that system from here.")
    assert is_cant_help("Sorry, I cannot access that.")
    assert is_cant_help("There's no tool for that on my side.")
    assert not is_cant_help("The backlog is 5.6M and growing.")
    assert not is_cant_help("I can't be sure, but the deploy went out at 14:00.")
    assert not is_cant_help("x" * 400 + " I can't see the threshold.")


async def test_a_concurrent_second_ask_to_the_same_colleague_is_not_dispatched(monkeypatch, ledger):
    import asyncio

    class SlowClient(FakeHTTPClient):
        async def post(self, url, json=None, headers=None, timeout=None):
            await asyncio.sleep(0)  # yield, as a real request would
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = SlowClient(post_responses=[_answer("ok")])
    provider = make_provider(client)
    first, second = await asyncio.gather(_ask(provider, "rin"), _ask(provider, "rin"))
    assert _asked(client) == ["rin"]
    assert first == "ok" and "already in flight" in second


async def test_without_a_ledger_the_guards_stand_aside(monkeypatch):
    """The revision pass asks through call() too, outside any answer's ledger."""
    from daedalus.tools.colleagues import ask_ledger

    token = ask_ledger.set(None)
    try:
        monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
        client = FakeHTTPClient(post_responses=[_answer(CANT)] * 5)
        provider = make_provider(client)
        for _ in range(5):
            await _ask(provider, "rin")
        assert len(client.post_calls) == 5
    finally:
        ask_ledger.reset(token)


async def test_list_and_remember_are_not_limited(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(
        get_responses=[FakeResponse(200, DIRECTORY)], post_responses=[_answer(CANT)]
    )
    provider = make_provider(client)
    await _ask(provider, "rin")
    listing = await provider.call("colleagues.list_colleagues", {})
    assert "rin" in listing


async def test_handle_task_gives_each_answer_a_fresh_ledger(monkeypatch):
    """End to end: the cap holds within one handle_task and resets for the next."""
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from daedalus.protocols import LLMResponse, ToolCall
    from daedalus.task import Task
    from tests.fakes import ScriptedLLM

    def asks(*names):
        return LLMResponse(
            text="",
            tool_calls=[
                ToolCall(id=n, name="colleagues__ask_colleague",
                         arguments={"name": n, "question": "q"})
                for n in names
            ],
        )

    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[_answer("ok")] * 4)
    provider = make_provider(client)
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "agent.yaml").write_text(
            "name: me\nmodel:\n  id: m\ntools:\n  provider: none\n  allow:\n"
            "    - colleagues.*\n"
        )
        (root / "agent-card.json").write_text("{}")
        (root / "persona.md").write_text("You are me.")
        llm = ScriptedLLM(asks("rin", "ana", "bo", "cy"), "first answer", asks("cy"), "second")
        agent = Agent(Bundle.load(root), llm=llm, tools=provider)
        await agent.handle_task(Task.from_text("one"))
        assert _asked(client) == ["rin", "ana", "bo"]
        await agent.handle_task(Task.from_text("two"))
        assert _asked(client) == ["rin", "ana", "bo", "cy"]


# --- 4xx refusals (#155, #56) ------------------------------------------------


@pytest.mark.asyncio
async def test_a_403_carries_the_switchboards_reason(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    reason = "agent:me is not a colleague of rin yet"
    client = FakeHTTPClient(post_responses=[FakeResponse(403, {"detail": reason})])
    text = await make_provider(client)._ask_colleague("rin", "q")
    assert reason in text
    assert "refused" in text
    assert "HTTP 403" in text


@pytest.mark.asyncio
async def test_a_4xx_with_a_non_json_body_uses_the_bounded_raw_text(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    body = "no " * 500
    client = FakeHTTPClient(post_responses=[FakeResponse(422, malformed=True, text=body)])
    text = await make_provider(client)._ask_colleague("rin", "q")
    assert "no no no" in text
    assert len(text) < 600
    assert "will be refused the same way" not in text


@pytest.mark.asyncio
async def test_a_503_keeps_the_transport_wording(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(503, {"detail": "upstream gone"}, text="x")]
    )
    text = await make_provider(client)._ask_colleague("rin", "q")
    assert text == "Asking rin failed: HTTP 503."


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_policy_refusal_never_marks_a_colleague_down(monkeypatch, status):
    """#56: two policy refusals used to ban a healthy colleague for five
    minutes. Three in a row must all reach the network."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(status, {"detail": "no"})] * 3
    )
    provider = make_provider(client)
    for _ in range(3):
        text = await provider._ask_colleague("rin", "q")
        assert "stopped trying" not in text
    assert len(client.post_calls) == 3
    assert "rin" not in provider._failures


@pytest.mark.asyncio
async def test_a_policy_refusal_does_not_clear_a_transport_failure(monkeypatch):
    """A 403 says nothing about whether the colleague is up, so it neither
    counts toward the ban nor clears a failure already recorded."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(503), FakeResponse(403), FakeResponse(503)]
    )
    provider = make_provider(client)
    await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")
    assert "stopped trying" in await provider._ask_colleague("rin", "q")
    assert len(client.post_calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422, 429])
async def test_a_non_policy_4xx_still_counts_toward_the_ban(monkeypatch, status):
    """The other half of #56: only 401/403/404 are exempt. Two of any other
    4xx ban the colleague, like two 503s."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(post_responses=[FakeResponse(status, {"detail": "slow down"})] * 2)
    provider = make_provider(client)
    first = await provider._ask_colleague("rin", "q")
    await provider._ask_colleague("rin", "q")
    assert "slow down" in first and "rejected" in first
    assert "stopped trying" in await provider._ask_colleague("rin", "q")
    assert len(client.post_calls) == 2


@pytest.mark.asyncio
async def test_a_reason_without_punctuation_gets_a_full_stop(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(403, {"detail": "agent:me is not a colleague of rin yet"})]
    )
    text = await make_provider(client)._ask_colleague("rin", "q")
    assert "rin yet. This is a refusal" in text


@pytest.mark.asyncio
async def test_an_empty_json_detail_is_not_echoed_as_raw_text(monkeypatch):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "t")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(403, {"detail": ""}, text='{"detail": ""}')]
    )
    text = await make_provider(client)._ask_colleague("rin", "q")
    assert "detail" not in text
    assert text.startswith("The request to ask rin was refused (HTTP 403).")


# --- #151: only asks that reached the colleague use a slot --------------------


async def test_after_a_transport_failure_a_retry_is_dispatched(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[FakeResponse(502), _answer("ok")])
    provider = make_provider(client)
    first = await _ask(provider, "rin")
    second = await _ask(provider, "rin")
    assert "HTTP 502" in first
    assert second == "ok"
    assert _asked(client) == ["rin", "rin"]
    assert ledger.asks == 1


async def test_failed_asks_give_their_slot_back(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(
        post_responses=[FakeResponse(503), FakeResponse(429)] + [_answer("ok")] * 3
    )
    provider = make_provider(client)
    await _ask(provider, "rin")
    await _ask(provider, "ana")
    for name in ("rin", "ana", "bo"):
        assert await _ask(provider, name) == "ok"
    assert _asked(client) == ["rin", "ana", "rin", "ana", "bo"]


async def test_asking_yourself_does_not_use_a_slot(monkeypatch, ledger):
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[_answer("ok")] * 3)
    provider = make_provider(client)
    assert "cannot ask myself" in await _ask(provider, "me")
    for name in ("rin", "ana", "bo"):
        await _ask(provider, name)
    assert _asked(client) == ["rin", "ana", "bo"]
    assert ledger.asks == 3


async def test_a_policy_refusal_is_cached_and_counted(monkeypatch, ledger):
    """A 403 won't change on a retry, so it stays in the ledger: a repeat is
    answered from it, and it uses a slot so refusals can't fuel a hunt."""
    monkeypatch.setenv("COLLEAGUE_TOKEN", "secret")
    client = FakeHTTPClient(post_responses=[FakeResponse(403, {"detail": "not a colleague"})])
    provider = make_provider(client)
    await _ask(provider, "rin")
    again = await _ask(provider, "rin")
    assert "already asked rin" in again and "not a colleague" in again
    assert _asked(client) == ["rin"]
    assert ledger.asks == 1
