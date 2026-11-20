"""Clicks and slash commands: the two things Slack sends that are not events.
Against the same fake API seam as test_transport_slack.py; no slack-sdk needed."""

import asyncio

import pytest

from daedalus.tools.colleagues import ASKED_BY_KEY, DEPTH_KEY
from daedalus.transports.slack import (
    CHOICES_BLOCK_ID,
    WAKE_THREAD,
    SlackTransport,
    current_slack_conversation,
    current_slack_requester,
)
from tests.test_transport_slack import CH, SELF_BOT, SELF_ID, AddressingApi, MarkHandler, event


class InteractingApi(AddressingApi):
    def __init__(self):
        super().__init__()
        self.settled: list[tuple[str, str, str]] = []
        self.responses: list[tuple[object, str]] = []
        self.refuse_posts = False

    async def post(self, channel, text, thread_ts=None):
        if self.refuse_posts:
            raise RuntimeError("channel_not_found")
        return await super().post(channel, text, thread_ts)

    async def settle(self, channel, ts, text):
        self.settled.append((channel, ts, text))

    async def respond(self, response_url, text):
        self.responses.append((response_url, text))


class SeeingHandler(MarkHandler):
    """Also records where and for whom the turn says it is running."""

    def __init__(self):
        super().__init__()
        self.seen: list[tuple] = []

    async def handle_task(self, task):
        self.seen.append((current_slack_conversation.get(), current_slack_requester.get()))
        return await super().handle_task(task)


def make(**kwargs):
    api = InteractingApi()
    transport = SlackTransport(CH, bot_token="xoxb", app_token="xapp", api=api, **kwargs)
    transport.user_id, transport.bot_id = SELF_ID, SELF_BOT
    return transport, api, SeeingHandler()


def click(value="prod", ts="50.5", thread="50.0", block_id=CHOICES_BLOCK_ID, trigger="t1", **extra):
    """A block_actions payload as Slack posts it for a button on a message."""
    return {
        "type": "block_actions",
        "trigger_id": trigger,
        "user": {"id": "U1"},
        "channel": {"id": CH},
        "container": {"type": "message", "message_ts": ts, "channel_id": CH},
        "message": {
            "ts": ts,
            "thread_ts": thread,
            "text": "Which one?",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Which one?"}},
                {"type": "actions", "block_id": block_id, "elements": []},
            ],
        },
        "actions": [
            {
                "type": "button",
                "block_id": block_id,
                "action_id": "daedalus_choice_1",
                "value": value,
            }
        ],
        **extra,
    }


def command(text="what changed today?", channel=CH, trigger="t9", **extra):
    """A slash command as the relay forwards it: Slack's form fields plus `type`."""
    return {
        "type": "slash_command",
        "command": "/helper",
        "text": text,
        "user_id": "U1",
        "channel_id": channel,
        "trigger_id": trigger,
        "response_url": "https://hooks.slack.com/commands/T0/1/abc",
        **extra,
    }


async def test_a_pick_settles_the_message_and_runs_as_that_persons_answer():
    transport, api, handler = make()
    await transport.handle_interaction(click(), handler)
    assert api.settled == [(CH, "50.5", "Which one?\n:white_check_mark: <@U1> chose *prod*")]
    asked = handler.tasks[0].history[0]
    assert asked.sender == "U1" and asked.text.endswith("\nprod")
    assert "You had asked: Which one?" in asked.text
    assert handler.tasks[0].context_id == f"slack:{CH}:50.0"
    assert api.updates[-1][3] == "50.0"  # the answer lands in the thread
    # A person's act: every tool, and the tools know where they are.
    assert DEPTH_KEY not in handler.marks[0] and ASKED_BY_KEY not in handler.marks[0]
    assert handler.seen == [((CH, "50.0", "50.5"), "U1")]
    assert current_slack_conversation.get() is None


async def test_the_first_pick_wins_and_a_click_on_anything_else_is_dropped():
    transport, api, handler = make()
    await transport.handle_interaction(click(trigger="t1"), handler)
    await transport.handle_interaction(click(value="dev", trigger="t2"), handler)
    await transport.handle_interaction(click(ts="60.5", block_id="someone_elses"), handler)
    assert len(handler.tasks) == 1 and len(api.settled) == 1


async def test_a_menu_pick_reads_the_selected_option():
    transport, _, handler = make()
    payload = click()
    payload["actions"] = [
        {
            "type": "static_select",
            "block_id": CHOICES_BLOCK_ID,
            "action_id": "daedalus_choice",
            "selected_option": {"text": {"type": "plain_text", "text": "env-7"}, "value": "env-7"},
        }
    ]
    await transport.handle_interaction(payload, handler)
    assert handler.tasks[0].history[0].text.endswith("\nenv-7")


async def test_a_slash_command_opens_a_thread_saying_who_asked_what():
    transport, api, handler = make(wake=WAKE_THREAD)
    await transport.handle_interaction(command(), handler)
    channel, opened, thread_ts = api.posts[0]
    assert (channel, thread_ts) == (CH, None)
    assert opened == "<@U1> asked with `/helper`:\n> what changed today?"
    asked = handler.tasks[0].history[0]
    assert (asked.sender, asked.text) == ("U1", "what changed today?")
    assert handler.tasks[0].context_id == f"slack:{CH}:ts-1"
    assert DEPTH_KEY not in handler.marks[0]
    # In thread mode the thread it opened is followed without a mention.
    await transport.handle_event(
        event("and yesterday?", ts="300.1", thread="ts-1", channel_type="channel"), handler
    )
    assert len(handler.tasks) == 2


async def test_a_command_it_cannot_run_is_explained_to_that_person_alone():
    transport, api, handler = make()
    await transport.handle_interaction(command(text="  ", trigger="a"), handler)
    api.refuse_posts = True
    await transport.handle_interaction(command(trigger="b"), handler)
    listed = SlackTransport("C0ONLY", bot_token="xoxb", app_token="xapp", api=api)
    await listed.handle_interaction(command(trigger="c"), handler)
    api.refuse_posts, api.member = False, False
    await transport.handle_interaction(command(trigger="d"), handler)
    assert handler.tasks == [] and api.posts == []
    assert [text.split(".")[0] for _, text in api.responses] == [
        "Type what you want after the command, like `/helper what changed today?`",
        "I cannot post in this conversation",
        "I do not answer in this channel",
        "I am not in this conversation",
    ]


async def test_a_command_where_it_was_never_added_runs_only_when_switched_on(monkeypatch):
    monkeypatch.setenv("SLACK_COMMAND_PUBLIC", "on")
    transport, api, handler = make()
    api.member = False
    await transport.handle_interaction(command(), handler)
    assert len(handler.tasks) == 1 and api.responses == []


async def test_a_pick_and_a_command_say_who_sent_them():
    """Both are a person's act, so both carry the person's label for the model;
    the pick's own note no longer repeats the Slack id."""
    transport, api, handler = make()

    async def who(user):
        return False, "Ada Lovelace", "ada@example.com"

    api.who = who
    await transport.handle_interaction(click(), handler)
    await transport.handle_interaction(command(), handler)
    picked, asked = (task.history[0] for task in handler.tasks)
    assert picked.sender_label == asked.sender_label == "Ada Lovelace <ada@example.com>"
    assert picked.text.startswith("(They picked one of the choices you offered in Slack.")
    assert asked.text == "what changed today?"


async def test_being_in_a_conversation_is_slacks_word_for_it():
    from daedalus.transports.slack import SlackApi

    class Web:
        async def conversations_info(self, channel):
            if channel == "C0GONE":
                raise RuntimeError("channel_not_found")
            known = {"C0IN": {"is_member": True}, "D0ME": {"is_im": True}}
            return {"channel": known.get(channel, {"is_member": False})}

    api = SlackApi(web_client=Web())
    asked = ("C0IN", "D0ME", "C0OUT", "C0GONE")
    assert [await api.in_conversation(c) for c in asked] == [True, True, False, False]


async def test_the_http_way_in_takes_both_and_runs_a_repeat_once():
    transport, api, handler = make()
    transport._started.set()
    assert transport.receive(command(), handler) is True
    assert transport.receive(command(), handler) is False  # the same trigger_id
    assert transport.receive(click(), handler) is True
    assert transport.receive({"type": "view_submission", "trigger_id": "v"}, handler) is False
    await asyncio.gather(*transport._inflight)
    assert len(handler.tasks) == 2


async def test_a_private_answer_only_ever_goes_to_slacks_own_host(monkeypatch):
    slack_sdk = pytest.importorskip("slack_sdk.webhook.async_client")
    sent: list[tuple[str, dict]] = []

    class Hook:
        def __init__(self, url):
            self.url = url

        async def send(self, **kwargs):
            sent.append((self.url, kwargs))

    monkeypatch.setattr(slack_sdk, "AsyncWebhookClient", Hook)
    from daedalus.transports.slack import SlackApi

    api = SlackApi(web_client=None)
    await api.respond("https://evil.example/hook", "hi")
    await api.respond(None, "hi")
    await api.respond("https://hooks.slack.com/commands/T0/1/abc", "hi")
    assert sent == [
        ("https://hooks.slack.com/commands/T0/1/abc", {"text": "hi", "response_type": "ephemeral"})
    ]
