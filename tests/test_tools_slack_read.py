"""SlackReadToolProvider: the read-only channel-thread sidecar. Every test
drives the provider through a fake async client -- no network, the bot
token is a monkeypatched env var. The shapes pinned here are the lane's
conduct: the bundle's channel allowlist is enforced on every call, the
scan respects its window and budget honestly, and every failure degrades
to plain tool-result text."""

import asyncio

import pytest

from daedalus.tools.slack_read import SlackReadToolProvider, _matches

NOW = 1_760_000_000.0  # fixed "now" for every test


class FakeSlack:
    """Answers Slack Web API GETs from scripted tables and records calls."""

    def __init__(self, history_pages=None, replies=None, permalink=None, errors=None):
        self.history_pages = list(history_pages or [])
        self.replies = replies or {}
        self.permalink = permalink
        self.errors = errors or {}  # method -> slack error string
        self.calls = []

    async def get(self, url, headers=None, params=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(params or {})))
        if method in self.errors:
            return _Resp({"ok": False, "error": self.errors[method]})
        if method == "conversations.history":
            page = self.history_pages.pop(0) if self.history_pages else {"messages": []}
            return _Resp({"ok": True, **page})
        if method == "conversations.replies":
            ts = (params or {}).get("ts")
            return _Resp({"ok": True, "messages": self.replies.get(ts, [])})
        if method == "chat.getPermalink":
            return _Resp({"ok": True, "permalink": self.permalink})
        raise AssertionError(f"unscripted slack method {method}")

    async def aclose(self):
        pass


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _provider(fake, monkeypatch, channels=("C0AAA",), **kwargs):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    return SlackReadToolProvider(
        list(channels), client=fake, now=lambda: NOW, **kwargs
    )


def _msg(ts_offset, text, thread_ts=None):
    ts = f"{NOW - ts_offset:.6f}"
    m = {"ts": ts, "text": text}
    if thread_ts:
        m["thread_ts"] = thread_ts
    return m


def test_matches_full_email_and_long_local_part_only():
    assert _matches("please grant <mailto:jdoe@acme.io|jdoe@acme.io> access", "jdoe@acme.io")
    assert _matches("please grant jdoe access", "jdoe@acme.io")
    assert not _matches("please grant someone access", "jdoe@acme.io")
    # A too-short local part must not fuzzy-match half the channel.
    assert not _matches("about ab testing", "ab@acme.io")


def test_find_threads_returns_thread_text_and_permalink(monkeypatch):
    root = _msg(3600, "access request for jdoe@acme.io")
    fake = FakeSlack(
        history_pages=[{"messages": [_msg(60, "unrelated"), root]}],
        replies={root["ts"]: [root, _msg(3000, "approved, ticket AREQ-42")]},
        permalink="https://slack.example/p1",
    )
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.find_threads", {"email": "jdoe@acme.io"}))
    assert "access request for jdoe@acme.io" in out
    assert "AREQ-42" in out
    assert "https://slack.example/p1" in out
    assert "2 messages" in out


def test_find_threads_refuses_channel_outside_allowlist(monkeypatch):
    provider = _provider(FakeSlack(), monkeypatch, channels=("C0AAA",))
    out = asyncio.run(
        provider.call("slack.find_threads", {"email": "j@a.io", "channel": "C0EVIL"})
    )
    assert "not on my allowed list" in out
    assert "C0AAA" in out
    assert not provider._client.calls  # refused before any network call


def test_find_threads_needs_explicit_channel_when_several_allowed(monkeypatch):
    provider = _provider(FakeSlack(), monkeypatch, channels=("C0AAA", "C0BBB"))
    out = asyncio.run(provider.call("slack.find_threads", {"email": "j@a.io"}))
    assert "which channel" in out
    assert "C0AAA" in out and "C0BBB" in out


def test_find_threads_without_token_degrades_politely(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    provider = SlackReadToolProvider(["C0AAA"], client=FakeSlack(), now=lambda: NOW)
    out = asyncio.run(provider.call("slack.find_threads", {"email": "j@a.io"}))
    assert "Could not read the channel history" in out
    assert "SLACK_BOT_TOKEN" in out


def test_find_threads_surfaces_slack_error_verbatim(monkeypatch):
    fake = FakeSlack(errors={"conversations.history": "not_in_channel"})
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.find_threads", {"email": "j@a.io"}))
    assert "not_in_channel" in out


def test_day_window_is_enforced_api_side(monkeypatch):
    fake = FakeSlack(history_pages=[{"messages": []}])
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(
        provider.call("slack.find_threads", {"email": "jdoe@acme.io", "days": 30})
    )
    method, params = fake.calls[0]
    assert method == "conversations.history"
    # The API's own oldest bound does the scoping -- no page-walking cutoff.
    assert abs(float(params["oldest"]) - (NOW - 30 * 86400)) < 2
    assert "latest" not in params
    assert "No threads mentioning jdoe@acme.io" in out
    assert "last 30 days" in out


def test_anchored_window_bounds_both_sides(monkeypatch):
    root = _msg(200 * 86400, "access request for jdoe@acme.io")
    fake = FakeSlack(
        history_pages=[{"messages": [root]}],
        replies={root["ts"]: [root]},
        permalink="https://slack.example/p9",
    )
    provider = _provider(fake, monkeypatch)
    from datetime import UTC, datetime

    anchor = datetime.fromtimestamp(NOW - 200 * 86400, tz=UTC).strftime("%Y-%m-%d")
    out = asyncio.run(
        provider.call(
            "slack.find_threads",
            {"email": "jdoe@acme.io", "around": anchor, "window_days": 5},
        )
    )
    _, params = fake.calls[0]
    assert float(params["latest"]) - float(params["oldest"]) == 10 * 86400
    # An anchored search finds the old thread the from-today scan never
    # reaches -- the whole point of anchoring on a known event time.
    assert "access request for jdoe@acme.io" in out
    assert "https://slack.example/p9" in out


def test_anchored_window_reports_its_reach_on_no_match(monkeypatch):
    fake = FakeSlack(history_pages=[{"messages": []}])
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(
        provider.call(
            "slack.find_threads",
            {"email": "jdoe@acme.io", "around": "2026-07-10", "window_days": 3},
        )
    )
    assert "2026-07-10 +/- 3 days" in out


def test_unreadable_anchor_date_is_refused_before_any_call(monkeypatch):
    provider = _provider(FakeSlack(), monkeypatch)
    out = asyncio.run(
        provider.call(
            "slack.find_threads", {"email": "jdoe@acme.io", "around": "last july"}
        )
    )
    assert "could not read" in out
    assert not provider._client.calls


def test_find_threads_reports_scan_budget_when_exhausted(monkeypatch):
    pages = [
        {"messages": [_msg(60 + i, "noise")], "response_metadata": {"next_cursor": "c"}}
        for i in range(3)
    ]
    fake = FakeSlack(history_pages=pages)
    provider = _provider(fake, monkeypatch, scan_limit=3)
    out = asyncio.run(provider.call("slack.find_threads", {"email": "jdoe@acme.io"}))
    assert "scan budget" in out


def test_read_thread_joins_replies_and_enforces_allowlist(monkeypatch):
    fake = FakeSlack(
        replies={"171.0": [{"ts": "171.0", "text": "root"}, {"ts": "172.0", "text": "reply"}]},
        permalink="https://slack.example/p2",
    )
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(
        provider.call("slack.read_thread", {"channel": "C0AAA", "thread_ts": "171.0"})
    )
    assert "root" in out and "reply" in out and "https://slack.example/p2" in out

    refused = asyncio.run(
        provider.call("slack.read_thread", {"channel": "C0EVIL", "thread_ts": "171.0"})
    )
    assert "not on my allowed list" in refused


def test_thread_text_truncates_honestly(monkeypatch):
    long_reply = {"ts": "171.0", "text": "x" * 9_000}
    fake = FakeSlack(replies={"171.0": [long_reply]})
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(
        provider.call("slack.read_thread", {"channel": "C0AAA", "thread_ts": "171.0"})
    )
    assert "[thread truncated" in out


def test_unknown_tool_raises():
    provider = SlackReadToolProvider(["C0AAA"], client=FakeSlack())
    with pytest.raises(LookupError):
        asyncio.run(provider.call("slack.post_message", {}))


def test_tools_listing_names_the_allowed_channels():
    provider = SlackReadToolProvider(["C0AAA", "C0BBB"], client=FakeSlack())
    specs = asyncio.run(provider.tools())
    names = {s["name"] for s in specs}
    assert names == {"slack.find_threads", "slack.read_thread"}
    find = next(s for s in specs if s["name"] == "slack.find_threads")
    assert "C0AAA" in find["description"] and "C0BBB" in find["description"]


def test_bundle_wiring_composes_the_sidecar(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "tools:\n  provider: none\n"
        "  slack_read:\n    channels: [C0AAA]\n"
        "  allow:\n    - slack.find_threads\n    - slack.read_thread\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"))
    from daedalus.tools.slack_read import SlackReadToolProvider as P

    assert isinstance(agent.tools, P)
    assert agent.tools.channels == ["C0AAA"]
