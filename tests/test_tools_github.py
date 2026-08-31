"""GitHubToolProvider: the room PR broker's client half. Every test drives
the provider through a fake async client -- no network, no tokens beyond a
monkeypatched env var. The shapes pinned here are the two-wall model's
client-side conduct: mint per call, refuse repos outside the grant, stamp
the branch and footer, degrade politely on every failure."""

import asyncio
import base64

import pytest

from daedalus.tools.github import GitHubToolProvider, _slug


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    """Routes requests by (method, path-suffix) against a scripted table and
    records everything sent."""

    def __init__(self, routes):
        self.routes = routes  # list of (method, path_contains, response)
        self.sent = []  # (method, url, kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        for m, fragment, response in self.routes:
            if m == method and fragment in url:
                return response
        raise AssertionError(f"unscripted request: {method} {url}")

    async def aclose(self):
        pass


MINT_OK = FakeResponse(
    200, {"token": "ghs_x", "expires_at": "later", "repositories": ["acme/tools"]}
)


def _provider(routes, monkeypatch, token="agent-jwt"):
    monkeypatch.setenv("CABAL_COLLEAGUE_TOKEN", token)
    return GitHubToolProvider(
        "http://kobo.test",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="ward",
        client=FakeClient(routes),
    )


def _open(provider, repo="acme/tools", files=None):
    return asyncio.run(
        provider.call(
            "github.open_pr",
            {
                "repository": repo,
                "title": "Fix the flaky retry",
                "body": "It was flaky.",
                "files": files or [{"path": "fix.md", "content": "done"}],
            },
        )
    )


def test_slug_is_branch_safe_and_collision_resistant():
    a, b = _slug("Fix the Flaky Retry!!"), _slug("Fix the Flaky Retry!!")
    assert a.startswith("fix-the-flaky-retry-")
    assert a != b  # random suffix


def test_no_token_env_degrades_politely(monkeypatch):
    provider = GitHubToolProvider("http://kobo.test", token_env=None, self_name="ward")
    out = _open(provider)
    assert "not configured" in out


def test_broker_404_means_no_grants(monkeypatch):
    routes = [("POST", "/broker/agents/ward/github-token", FakeResponse(404))]
    provider = _provider(routes, monkeypatch)
    out = _open(provider)
    assert "has not granted me any repositories" in out


def test_repo_outside_grant_is_refused_client_side(monkeypatch):
    provider = _provider([("POST", "/broker/", MINT_OK)], monkeypatch)
    out = _open(provider, repo="acme/other")
    assert "not approved for acme/other" in out
    assert "acme/tools" in out  # names what IS granted


def test_open_pr_happy_path_stamps_branch_and_footer(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/git/ref/", FakeResponse(200, {"object": {"sha": "base123"}})),
        ("GET", "/repos/acme/tools/contents/", FakeResponse(404)),
        ("GET", "/repos/acme/tools", FakeResponse(200, {"default_branch": "main"})),
        ("POST", "/repos/acme/tools/git/refs", FakeResponse(201)),
        ("PUT", "/repos/acme/tools/contents/", FakeResponse(201)),
        ("POST", "/repos/acme/tools/pulls", FakeResponse(201, {"html_url": "https://github.test/pr/7"})),
    ]
    provider = _provider(routes, monkeypatch)
    out = _open(provider)
    assert "https://github.test/pr/7" in out

    client = provider._client
    ref_posts = [k for m, u, k in client.sent if m == "POST" and u.endswith("/git/refs")]
    assert ref_posts[0]["json"]["ref"].startswith("refs/heads/kobo/ward/fix-the-flaky-retry-")
    put_calls = [k for m, u, k in client.sent if m == "PUT"]
    assert base64.b64decode(put_calls[0]["json"]["content"]).decode() == "done"
    assert "sha" not in put_calls[0]["json"]  # fresh file: no blob sha sent
    pr_calls = [k for m, u, k in client.sent if m == "POST" and u.endswith("/pulls")]
    body = pr_calls[0]["json"]["body"]
    assert "Opened by agent **ward**" in body
    assert pr_calls[0]["json"]["base"] == "main"


def test_existing_file_update_carries_its_blob_sha(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/git/ref/", FakeResponse(200, {"object": {"sha": "base123"}})),
        ("GET", "/repos/acme/tools/contents/", FakeResponse(200, {"sha": "blob456"})),
        ("GET", "/repos/acme/tools", FakeResponse(200, {"default_branch": "main"})),
        ("POST", "/repos/acme/tools/git/refs", FakeResponse(201)),
        ("PUT", "/repos/acme/tools/contents/", FakeResponse(200)),
        ("POST", "/repos/acme/tools/pulls", FakeResponse(201, {"html_url": "u"})),
    ]
    provider = _provider(routes, monkeypatch)
    _open(provider)
    put_calls = [k for m, u, k in provider._client.sent if m == "PUT"]
    assert put_calls[0]["json"]["sha"] == "blob456"


def test_repo_not_in_installation_reads_as_helpful_text(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools", FakeResponse(404)),
    ]
    provider = _provider(routes, monkeypatch)
    out = _open(provider)
    assert "app installation" in out


def test_unknown_tool_raises_lookup():
    provider = GitHubToolProvider("http://kobo.test")
    with pytest.raises(LookupError):
        asyncio.run(provider.call("github.merge", {}))
