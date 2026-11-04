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

    def __init__(self, history_pages=None, replies=None, permalink=None, errors=None, users=None):
        self.history_pages = list(history_pages or [])
        self.replies = replies or {}
        self.permalink = permalink
        self.errors = errors or {}  # method -> slack error string
        # Slack id -> {"real_name", "email"}. None = the app lacks users:read
        # (and users:read.email), the way most bots are installed.
        self.users = users
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
        if method in ("users.info", "users.lookupByEmail"):
            if self.users is None:
                return _Resp({"ok": False, "error": "missing_scope"})
            if method == "users.info":
                u = self.users.get((params or {}).get("user"))
                if u is None:
                    return _Resp({"ok": False, "error": "user_not_found"})
                return _Resp({"ok": True, "user": {"id": params["user"], "profile": u}})
            email = (params or {}).get("email")
            uid = next((k for k, u in self.users.items() if u.get("email") == email), None)
            if uid is None:
                return _Resp({"ok": False, "error": "users_not_found"})
            return _Resp({"ok": True, "user": {"id": uid}})
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
    method, params = next(c for c in fake.calls if c[0] == "conversations.history")
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
    _, params = next(c for c in fake.calls if c[0] == "conversations.history")
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


# --- an empty allowlist means every channel the app was invited to -----------
#
# 2026-09-23 operator ruling, the read lane's half of it. The bundle may turn
# the lane on without naming channels; Slack's own membership is then the
# wall, and its `not_in_channel` is the refusal the agent reads.


def test_empty_allowlist_reads_a_channel_it_never_listed(monkeypatch):
    fake = FakeSlack(
        replies={"171.0": [{"ts": "171.0", "text": "root"}]},
        permalink="https://slack.example/p3",
    )
    provider = _provider(fake, monkeypatch, channels=())
    out = asyncio.run(
        provider.call("slack.read_thread", {"channel": "C0ANY", "thread_ts": "171.0"})
    )
    assert "root" in out
    assert "not on my allowed list" not in out


def test_empty_allowlist_lets_slack_be_the_refusal(monkeypatch):
    """No membership means no read -- but the wall is Slack's, and the agent
    is told so in the lane's own plain sentence."""
    fake = FakeSlack(errors={"conversations.replies": "not_in_channel"})
    provider = _provider(fake, monkeypatch, channels=())
    out = asyncio.run(
        provider.call("slack.read_thread", {"channel": "C0CLOSED", "thread_ts": "171.0"})
    )
    assert "Could not read that thread" in out
    assert "not_in_channel" in out


def test_empty_allowlist_asks_which_channel_without_claiming_none(monkeypatch):
    provider = _provider(FakeSlack(), monkeypatch, channels=())
    out = asyncio.run(provider.call("slack.find_threads", {"email": "j@a.io"}))
    assert "which channel" in out
    assert "every channel the app is invited to" in out
    assert "none" not in out.lower()
    assert not provider._client.calls


def test_tools_listing_says_every_invited_channel_when_open():
    provider = SlackReadToolProvider([], client=FakeSlack())
    find = next(s for s in asyncio.run(provider.tools()) if s["name"] == "slack.find_threads")
    assert "every channel the app is invited to" in find["description"]
    assert "none" not in find["description"].lower()


def test_bundle_wiring_opens_the_lane_on_an_empty_channel_list(tmp_path, capsys):
    """`channels: []` written out loud turns the lane ON with no allowlist;
    omitting the block entirely still leaves it OFF."""
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import ScriptedLLM

    def _bundle(name: str, slack_read: str):
        home = tmp_path / name
        home.mkdir()
        (home / "agent.yaml").write_text(
            "name: x\nmodel:\n  id: m\n"
            "tools:\n  provider: none\n"
            f"{slack_read}"
            "  allow:\n    - slack.find_threads\n    - slack.read_thread\n"
        )
        (home / "agent-card.json").write_text("{}")
        (home / "persona.md").write_text("You are x.")
        return Agent(Bundle.load(home), llm=ScriptedLLM("done"))

    opened = _bundle("open", "  slack_read:\n    channels: []\n")
    assert isinstance(opened.tools, SlackReadToolProvider)
    assert opened.tools.channels == []
    # `channels: []` meant OFF before 2026-09-23; boot says it is now ON.
    assert "slack read lane open" in capsys.readouterr().out

    absent = _bundle("off", "")
    assert not isinstance(absent.tools, SlackReadToolProvider)


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


@pytest.mark.parametrize("arg", [{"days": "a month"}, {"limit": "five"}, {"days": [30]}])
def test_unreadable_counts_read_as_defaults_not_errors(monkeypatch, arg):
    fake = FakeSlack(history_pages=[{"messages": []}])
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(
        provider.call("slack.find_threads", {"email": "jdoe@acme.io", **arg})
    )
    assert "No threads mentioning jdoe@acme.io" in out


def test_unreadable_window_reads_as_default(monkeypatch):
    fake = FakeSlack(history_pages=[{"messages": []}])
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.find_threads", {
        "email": "jdoe@acme.io", "around": "2026-07-10", "window_days": "wide"}))
    assert "2026-07-10 +/- 7 days" in out


def test_a_thread_that_names_the_person_only_in_a_reply_is_found(monkeypatch):
    """A request bot's root often names the requester as <@U...>; the email
    shows up only in a reply. A root-only scan answered "no thread" for a
    thread that existed (2026-11-04), so the find also reads the replies of
    the window's threads."""
    root = {**_msg(3600, "<@U0REQ> can I get edge cache access?"), "reply_count": 2}
    root["thread_ts"] = root["ts"]
    other = {**_msg(7200, "unrelated ask"), "reply_count": 1}
    other["thread_ts"] = other["ts"]
    fake = FakeSlack(
        history_pages=[{"messages": [root, other]}],
        replies={
            root["ts"]: [
                root,
                _msg(3500, "Plan: add <mailto:jdoe@acme.io|jdoe@acme.io> to the cdn group"),
            ],
            other["ts"]: [other, _msg(7100, "done")],
        },
        permalink="https://slack.example/p1",
    )
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.find_threads", {"email": "jdoe@acme.io"}))
    assert "named in a reply, not the opening message" in out
    assert "https://slack.example/p1" in out
    assert "unrelated ask" not in out


def test_a_thread_read_shows_reactions_and_who_reacted(monkeypatch):
    """The approval often IS a reaction (:+1: from an approver), so a read
    returns each message's reactions with the reacting users, and names them
    when the app may look people up."""
    root = {**_msg(3600, "<@U0REQ> please add me"), "user": "U0REQ",
            "reactions": [{"name": "+1", "users": ["U0VP"], "count": 1}]}
    fake = FakeSlack(
        replies={root["ts"]: [root]},
        users={"U0REQ": {"real_name": "Jo Doe", "email": "jdoe@acme.io"},
               "U0VP": {"real_name": "Vee Pee", "email": "vp@acme.io"}},
    )
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.read_thread", {"thread_ts": root["ts"]}))
    assert ":+1: by Vee Pee <vp@acme.io> (U0VP)" in out
    assert "Jo Doe <jdoe@acme.io> (U0REQ): @Jo Doe <jdoe@acme.io> (U0REQ) please add me" in out


def test_without_users_read_ids_stay_ids_and_the_read_says_why(monkeypatch):
    root = {**_msg(3600, "please add me"), "user": "U0REQ",
            "reactions": [{"name": "+1", "users": ["U0VP"], "count": 1}]}
    fake = FakeSlack(replies={root["ts"]: [root]})
    provider = _provider(fake, monkeypatch)
    out = asyncio.run(provider.call("slack.read_thread", {"thread_ts": root["ts"]}))
    assert ":+1: by U0VP" in out
    assert "lacks the users:read scope" in out
