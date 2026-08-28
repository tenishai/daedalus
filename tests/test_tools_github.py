"""GitHubToolProvider: the room PR broker's client half. Every test drives
the provider through a fake async client -- no network, no tokens beyond a
monkeypatched env var. The shapes pinned here are the two-wall model's
client-side conduct: mint per call, refuse repos outside the grant, stamp
the branch and footer, degrade politely on every failure."""

import asyncio
import base64
import json

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
    provider = _provider([("POST", "/broker/agents/ward/github-token", FakeResponse(404))], monkeypatch)
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


# --- the read quartet (2026-08-28): list_prs, get_pr, list_releases, ---------
# --- read_file. Same walls as open_pr: mint per call, refuse repos ----------
# --- outside the grant. New conduct pinned here: reads never write ----------
# --- (zero non-GET GitHub calls), and every bound announces itself. ---------


def _call(provider, name, **payload):
    return asyncio.run(provider.call(name, payload))


def _github_writes(provider):
    """Every non-GET request that reached GitHub (the broker POST is not
    GitHub)."""
    return [
        (m, u)
        for m, u, _ in provider._client.sent
        if m != "GET" and "/broker/" not in u
    ]


def test_list_prs_lists_and_marks_merged(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/pulls", FakeResponse(200, [
            {"number": 7, "state": "closed", "merged_at": "2026-08-12T10:00:00Z",
             "title": "Add retries", "user": {"login": "lyan"}},
            {"number": 6, "state": "open", "merged_at": None,
             "title": "WIP thing", "user": {"login": "kg"}},
        ])),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.list_prs", repository="acme/tools", state="all")
    assert "#7 [merged 2026-08-12] Add retries (lyan)" in out
    assert "#6 [open] WIP thing (kg)" in out
    assert _github_writes(provider) == []


def test_list_prs_merged_state_filters_unmerged_closed(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/pulls", FakeResponse(200, [
            {"number": 5, "state": "closed", "merged_at": None,
             "title": "Abandoned", "user": {"login": "kg"}},
            {"number": 4, "state": "closed", "merged_at": "2026-08-01T00:00:00Z",
             "title": "Landed", "user": {"login": "lyan"}},
        ])),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.list_prs", repository="acme/tools", state="merged")
    assert "Landed" in out
    assert "Abandoned" not in out
    # and the query itself asked GitHub for closed (merged is not a GitHub state)
    (list_call,) = [k for m, u, k in provider._client.sent if "/pulls" in u]
    assert list_call["params"]["state"] == "closed"


def test_list_prs_announces_its_clip(monkeypatch):
    pulls = [
        {"number": i, "state": "open", "merged_at": None, "title": f"t{i}",
         "user": {"login": "kg"}}
        for i in range(30)
    ]
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/pulls", FakeResponse(200, pulls)),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.list_prs", repository="acme/tools", limit=3)
    assert "only the first 3 shown" in out


def test_list_prs_refuses_repo_outside_grant(monkeypatch):
    provider = _provider([("POST", "/broker/", MINT_OK)], monkeypatch)
    out = _call(provider, "github.list_prs", repository="acme/other")
    assert "not approved for acme/other" in out
    assert _github_writes(provider) == []


def test_get_pr_carries_body_files_and_truncation_honesty(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/pulls/7/files", FakeResponse(200, [
            {"filename": "a.proto", "status": "modified", "additions": 2, "deletions": 2},
        ])),
        ("GET", "/repos/acme/tools/pulls/7", FakeResponse(200, {
            "title": "Rename a field", "state": "closed",
            "merged_at": "2026-08-12T10:00:00Z", "body": "x" * 5000,
            "user": {"login": "lyan"},
            "head": {"ref": "feat/rename"}, "base": {"ref": "main"},
        })),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.get_pr", repository="acme/tools", number=7)
    assert "#7 [merged 2026-08-12] Rename a field" in out
    assert "feat/rename -> main" in out
    assert "a.proto (modified, +2/-2)" in out
    assert "[description truncated]" in out
    assert _github_writes(provider) == []


def test_list_releases_prefers_releases(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/releases", FakeResponse(200, [
            {"tag_name": "v2.4.0", "name": "v2.4.0",
             "published_at": "2026-08-20T00:00:00Z", "prerelease": False},
        ])),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.list_releases", repository="acme/tools")
    assert out == "v2.4.0 (2026-08-20)"


def test_list_releases_falls_back_to_bare_tags(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/releases", FakeResponse(200, [])),
        ("GET", "/repos/acme/tools/tags", FakeResponse(200, [
            {"name": "v1.9.2", "commit": {"sha": "abcdef1234567"}},
        ])),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.list_releases", repository="acme/tools")
    assert "publishes no GitHub releases" in out
    assert "v1.9.2 (abcdef1)" in out


def test_read_file_decodes_and_marks_truncation(monkeypatch):
    import base64 as b64
    big = "line\n" * 20000  # 100k chars, past the 48k ceiling
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/contents/README.md", FakeResponse(200, {
            "type": "file", "encoding": "base64",
            "content": b64.b64encode(big.encode()).decode(),
        })),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.read_file", repository="acme/tools", path="README.md")
    assert out.startswith("line\n")
    assert out.endswith("[file truncated -- it continues past this point]")
    assert _github_writes(provider) == []


def test_read_file_names_a_directory_instead_of_failing(monkeypatch):
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/contents/protos", FakeResponse(200, [
            {"name": "user.proto"}, {"name": "event.proto"},
        ])),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.read_file", repository="acme/tools", path="protos")
    assert "protos is a directory" in out
    assert "user.proto" in out


def test_read_file_ref_rides_the_query(monkeypatch):
    import base64 as b64
    routes = [
        ("POST", "/broker/", MINT_OK),
        ("GET", "/repos/acme/tools/contents/VERSION", FakeResponse(200, {
            "type": "file", "encoding": "base64",
            "content": b64.b64encode(b"2.4.0").decode(),
        })),
    ]
    provider = _provider(routes, monkeypatch)
    out = _call(provider, "github.read_file", repository="acme/tools",
                path="VERSION", ref="v2.4.0")
    assert out == "2.4.0"
    (contents_call,) = [k for m, u, k in provider._client.sent if "/contents/" in u]
    assert contents_call["params"] == {"ref": "v2.4.0"}


def test_read_tools_are_declared(monkeypatch):
    provider = GitHubToolProvider("http://kobo.test")
    names = [t["name"] for t in asyncio.run(provider.tools())]
    assert names == [
        "github.open_pr",
        "github.list_prs",
        "github.get_pr",
        "github.list_releases",
        "github.read_file",
    ]
