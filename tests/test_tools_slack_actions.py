"""The slack-actions sidecar: what an agent in Slack can do besides answer.
Against a fake request client; no slack-sdk and no network."""

import pytest

from daedalus.agent import Agent
from daedalus.tools.slack_actions import SlackActionsToolProvider
from daedalus.transports.slack import (
    CHOICES_BLOCK_ID,
    current_slack_conversation,
    current_slack_requester,
)
from tests.fakes import ScriptedLLM

HERE = ("C0HERE000", "100.0", "100.5")  # channel, thread, the message that asked


class _Answer:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeSlack:
    """Records each Web API call and answers from a per-method script."""

    def __init__(self, **answers):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers

    async def post(self, url, headers=None, json=None, data=None):
        assert headers == {"Authorization": "Bearer xoxb-test"}
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json if json is not None else data))
        scripted = self.answers.get(method.replace(".", "_"))
        if callable(scripted):
            scripted = scripted(json if json is not None else data)
        return _Answer(scripted or {"ok": True, "ts": "200.1", "channel": "C0OTHER"})


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_ICON_URL", raising=False)
    conversation = current_slack_conversation.set(HERE)
    requester = current_slack_requester.set("U1")
    yield
    current_slack_conversation.reset(conversation)
    current_slack_requester.reset(requester)


def provider(channels=(), **answers):
    client = FakeSlack(**answers)
    return SlackActionsToolProvider(list(channels), client=client), client


async def test_it_lists_nothing_until_slack_is_connected(monkeypatch):
    """The room wires Slack dormant at birth: no token, no tools, no cost."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    tools, _ = provider()
    assert await tools.tools() == []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    assert [t["name"] for t in await tools.tools()] == [
        "slack.post_message",
        "slack.offer_choices",
        "slack.pins",
        "slack.bookmarks",
    ]
    assert not getattr(tools, "knowledge_only", False)  # never on a program's ask


async def test_a_message_posted_elsewhere_names_who_asked(slack):
    tools, client = provider()
    said = await tools.call("slack.post_message", {"channel": "#announce", "text": "**Shipped**"})
    assert client.calls == [
        ("chat.postMessage", {"channel": "#announce", "text": "*Shipped*\n_Sent for <@U1>_"})
    ]
    assert said == "Posted in C0OTHER (message 200.1)."


async def test_a_named_channel_list_is_the_reach_outside_the_conversation(slack):
    tools, client = provider(channels=["C0ALLOWED"])
    refused = await tools.call("slack.post_message", {"channel": "C0NOPE", "text": "hi"})
    assert "not on my allowed list" in refused and client.calls == []
    await tools.call("slack.post_message", {"channel": "C0ALLOWED", "text": "hi"})
    await tools.call("slack.post_message", {"text": "hi"})  # the conversation itself
    assert [c[1]["channel"] for c in client.calls] == ["C0ALLOWED", "C0HERE000"]


async def test_a_task_that_did_not_start_in_slack_must_name_the_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    tools, client = provider()
    assert "which channel" in await tools.call("slack.post_message", {"text": "hi"})
    await tools.call("slack.post_message", {"channel": "C0X", "text": "hi"})
    assert client.calls == [("chat.postMessage", {"channel": "C0X", "text": "hi"})]


async def test_choices_are_buttons_up_to_five_and_a_menu_past_that(slack):
    """The wire shape the transport's click handler reads back: the block id,
    and each element's value being the choice's own words."""
    tools, client = provider()
    await tools.call("slack.offer_choices", {"question": "Which one?", "choices": ["dev", "prod"]})
    body = client.calls[0][1]
    assert (body["channel"], body["thread_ts"], body["text"]) == (
        "C0HERE000",
        "100.0",
        "Which one?",
    )
    section, actions = body["blocks"]
    assert section == {"type": "section", "text": {"type": "mrkdwn", "text": "Which one?"}}
    assert actions["block_id"] == CHOICES_BLOCK_ID
    assert [(e["type"], e["value"]) for e in actions["elements"]] == [
        ("button", "dev"),
        ("button", "prod"),
    ]

    await tools.call(
        "slack.offer_choices", {"question": "Which?", "choices": [f"env-{i}" for i in range(6)]}
    )
    (menu,) = client.calls[1][1]["blocks"][1]["elements"]
    assert menu["type"] == "static_select"
    assert [o["value"] for o in menu["options"]] == [f"env-{i}" for i in range(6)]


async def test_choices_slack_would_refuse_are_refused_in_words(slack):
    tools, client = provider()
    assert "at least two" in await tools.call(
        "slack.offer_choices", {"question": "Sure?", "choices": ["yes", "yes"]}
    )
    assert "75 characters" in await tools.call(
        "slack.offer_choices", {"question": "Sure?", "choices": ["yes", "n" * 76]}
    )
    assert client.calls == []


async def test_pins_name_a_message_by_word_or_by_link(slack):
    tools, client = provider()
    await tools.call("slack.pins", {"action": "add"})
    await tools.call("slack.pins", {"action": "add", "message": "thread"})
    await tools.call(
        "slack.pins",
        {"action": "remove", "message": "https://x.slack.com/archives/C0LINK/p1725400000123456"},
    )
    assert client.calls == [
        ("pins.add", {"channel": "C0HERE000", "timestamp": "100.5"}),
        ("pins.add", {"channel": "C0HERE000", "timestamp": "100.0"}),
        ("pins.remove", {"channel": "C0LINK", "timestamp": "1725400000.123456"}),
    ]


async def test_bookmarks_add_list_and_remove(slack):
    tools, client = provider(
        bookmarks_add={"ok": True, "bookmark": {"id": "Bk1"}},
        bookmarks_list={
            "ok": True,
            "bookmarks": [{"id": "Bk1", "title": "Runbook", "link": "https://r"}],
        },
    )
    added = await tools.call(
        "slack.bookmarks", {"action": "add", "title": "Runbook", "link": "https://r"}
    )
    listed = await tools.call("slack.bookmarks", {"action": "list"})
    await tools.call("slack.bookmarks", {"action": "remove", "bookmark_id": "Bk1"})
    assert "bookmark_id Bk1" in added and "Runbook: https://r (bookmark_id Bk1)" in listed
    assert client.calls == [
        (
            "bookmarks.add",
            {"channel_id": "C0HERE000", "title": "Runbook", "type": "link", "link": "https://r"},
        ),
        ("bookmarks.list", {"channel_id": "C0HERE000"}),
        ("bookmarks.remove", {"channel_id": "C0HERE000", "bookmark_id": "Bk1"}),
    ]


async def test_slacks_refusals_come_back_as_sentences_never_exceptions(slack):
    tools, _ = provider(
        pins_add={"ok": False, "error": "missing_scope", "needed": "pins:write"},
        chat_postMessage={"ok": False, "error": "not_in_channel"},
    )
    assert "missing `pins:write`" in await tools.call("slack.pins", {"action": "add"})
    assert "not in that channel" in await tools.call(
        "slack.post_message", {"channel": "C0PRIVATE", "text": "hi"}
    )

    class Down:
        async def post(self, *args, **kwargs):
            raise OSError("no route")

    broken = SlackActionsToolProvider([], client=Down())
    assert "could not be reached" in await broken.call("slack.bookmarks", {"action": "list"})


async def test_an_app_that_refuses_the_picture_posts_plain_from_then_on(slack, monkeypatch):
    monkeypatch.setenv("SLACK_ICON_URL", "https://room/face.png")
    tools, client = provider(
        chat_postMessage=lambda body: {
            "ok": "icon_url" not in body,
            "error": "missing_scope",
            "ts": "1.0",
        }
    )
    await tools.call("slack.post_message", {"text": "one"})
    await tools.call("slack.post_message", {"text": "two"})
    assert ["icon_url" in body for _, body in client.calls] == [True, False, False]


def test_a_slack_transport_block_turns_the_tools_on(tmp_path):
    """The block is the switch; the channels it names are the reach."""

    def providers_of(yaml: str) -> list:
        (tmp_path / "agent.yaml").write_text(yaml)
        (tmp_path / "agent-card.json").write_text("{}")
        (tmp_path / "persona.md").write_text("persona")
        tools = Agent.load(tmp_path, llm=ScriptedLLM()).tools
        return [
            p
            for p in getattr(tools, "providers", [tools])
            if isinstance(p, SlackActionsToolProvider)
        ]

    base = "name: x\nmodel:\n  id: m\ntransports:\n"
    assert providers_of(base + "  - kind: cli\n") == []
    assert providers_of(base + "  - kind: slack\n    channel: C0123\n")[0].channels == ["C0123"]
    # One block that names no channel opens the reach, as it does the transport's.
    assert (
        providers_of(base + "  - kind: slack\n    channel: C0123\n  - kind: slack\n")[0].channels
        == []
    )
