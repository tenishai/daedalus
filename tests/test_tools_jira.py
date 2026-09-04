"""JiraToolProvider: the room broker's issue-tracker read lane, client
half. Every test drives the provider through a fake async client -- no
network, the colleague JWT is a monkeypatched env var. The shapes pinned
here mirror the github sidecar's conduct: broker-authenticated reads,
404 as the no-grant answer, honest truncation, polite degrades."""

import asyncio

import pytest

from daedalus.tools.jira import JiraToolProvider


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeBroker:
    def __init__(self, routes):
        self.routes = routes  # list of (path_fragment, response-or-exception)
        self.sent = []  # (url, headers, params)

    async def get(self, url, headers=None, params=None):
        self.sent.append((url, dict(headers or {}), dict(params or {})))
        for fragment, response in self.routes:
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unscripted broker call: {url}")

    async def aclose(self):
        pass


ISSUE_OK = _Resp(
    200,
    {
        "issue": {
            "key": "AREQ-42",
            "summary": "Access to the payments dashboard",
            "status": "Done",
            "type": "Access Request",
            "reporter": "jdoe@acme.io",
            "assignee": None,
            "resolution": "Done",
            "resolved_at": "2026-09-01T10:00:00Z",
            "created_at": "2026-08-30T09:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z",
            "labels": ["quarterly-audit"],
            "url": "https://tracker.example/browse/AREQ-42",
            "description": "Please grant dashboard access.",
        },
        "comments": [
            {
                "author": "V. President",
                "author_email": "vp@acme.io",
                "created": "2026-08-31T12:00:00Z",
                "body": "Approved.",
            }
        ],
    },
)


def _provider(routes, monkeypatch, token="agent-jwt"):
    monkeypatch.setenv("CABAL_COLLEAGUE_TOKEN", token)
    return JiraToolProvider(
        "http://kobo.test",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="ledger",
        client=FakeBroker(routes),
    )


def test_get_issue_formats_fields_and_comments(monkeypatch):
    provider = _provider([("/jira/issues/AREQ-42", ISSUE_OK)], monkeypatch)
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-42"}))
    assert "AREQ-42: Access to the payments dashboard" in out
    assert "status: Done (Access Request)" in out
    assert "reporter: jdoe@acme.io" in out
    assert "labels: quarterly-audit" in out
    assert "V. President <vp@acme.io> at 2026-08-31T12:00:00Z" in out
    assert "Approved." in out
    # The route was called with the agent's own bearer, on the agent's path.
    url, headers, _ = provider._client.sent[0]
    assert "/broker/agents/ledger/jira/issues/AREQ-42" in url
    assert headers["Authorization"] == "Bearer agent-jwt"


def test_get_issue_404_reads_as_no_grant(monkeypatch):
    provider = _provider([("/jira/issues", _Resp(404))], monkeypatch)
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-1"}))
    assert "has not granted me issue-tracker access" in out


def test_without_token_env_degrades_politely(monkeypatch):
    monkeypatch.delenv("CABAL_COLLEAGUE_TOKEN", raising=False)
    provider = JiraToolProvider(
        "http://kobo.test",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="ledger",
        client=FakeBroker([]),
    )
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-1"}))
    assert "not configured" in out
    assert not provider._client.sent


def test_unreachable_broker_degrades_politely(monkeypatch):
    provider = _provider(
        [("/jira/issues", ConnectionError("boom"))], monkeypatch
    )
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-1"}))
    assert "Could not reach the room's broker" in out


def test_refused_status_is_named_by_code(monkeypatch):
    provider = _provider([("/jira/issues", _Resp(503))], monkeypatch)
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-1"}))
    assert "HTTP 503" in out


def test_long_comment_truncates_honestly(monkeypatch):
    body = {
        "issue": {"key": "AREQ-9", "summary": "s"},
        "comments": [{"author": "a", "created": "t", "body": "y" * 3_000}],
    }
    provider = _provider([("/jira/issues", _Resp(200, body))], monkeypatch)
    out = asyncio.run(provider.call("jira.get_issue", {"key": "AREQ-9"}))
    assert "[comment truncated]" in out


def test_find_issues_lists_and_marks_truncation(monkeypatch):
    body = {
        "issues": [
            {
                "key": "AREQ-42",
                "status": "Done",
                "summary": "Access to the payments dashboard",
                "reporter": "jdoe@acme.io",
                "created_at": "2026-08-30T09:00:00Z",
            }
        ],
        "truncated": True,
    }
    provider = _provider([("/jira/search", _Resp(200, body))], monkeypatch)
    out = asyncio.run(
        provider.call("jira.find_issues", {"text": "jdoe@acme.io", "limit": 1})
    )
    assert "AREQ-42 [Done] Access to the payments dashboard" in out
    assert "(reporter jdoe@acme.io, created 2026-08-30)" in out
    assert "only the first 1 shown" in out
    _, _, params = provider._client.sent[0]
    assert params == {"text": "jdoe@acme.io", "limit": 1}


def test_find_issues_needs_a_real_term(monkeypatch):
    provider = _provider([], monkeypatch)
    out = asyncio.run(provider.call("jira.find_issues", {"text": "a"}))
    assert "at least a few characters" in out
    assert not provider._client.sent


def test_unknown_tool_raises(monkeypatch):
    provider = _provider([], monkeypatch)
    with pytest.raises(LookupError):
        asyncio.run(provider.call("jira.create_issue", {}))


def test_bundle_wiring_composes_the_sidecar(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: none\n"
        "  jira:\n    broker_url: http://kobo.test\n"
        "    token_env: CABAL_COLLEAGUE_TOKEN\n    self: x\n"
        "  allow:\n    - jira.get_issue\n    - jira.find_issues\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"))
    from daedalus.tools.jira import JiraToolProvider as P

    assert isinstance(agent.tools, P)
    assert agent.tools.self_name == "x"


def test_find_issues_passes_the_created_window_through(monkeypatch):
    provider = _provider(
        [("/jira/search", _Resp(200, {"issues": [], "truncated": False}))], monkeypatch
    )
    asyncio.run(
        provider.call(
            "jira.find_issues",
            {
                "text": "jdoe@acme.io",
                "created_after": "2026-07-03",
                "created_before": "2026-07-17",
            },
        )
    )
    _, _, params = provider._client.sent[0]
    assert params["created_after"] == "2026-07-03"
    assert params["created_before"] == "2026-07-17"
