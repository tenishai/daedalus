"""Slack transport logic — the battle scars, replayed as tests.
All against a fake API seam; no slack-sdk needed."""

import asyncio

from daedalus.protocols import DenyAll
from daedalus.task import ReasonCode, Task, TaskState
from daedalus.transports.slack import SlackReactionApprover, SlackTransport

CH = "C0MAIN"


class FakeApi:
    def __init__(self, reactions_script=None):
        self.posts: list[tuple[str, str, str | None]] = []
        self.reacts: list[tuple[str, str]] = []
        self.unreacts: list[tuple[str, str]] = []
        self._reactions_script = list(reactions_script or [])
        self._ts = 0

    async def post(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))
        self._ts += 1
        return f"ts-{self._ts}"

    async def react(self, channel, ts, name):
        self.reacts.append((ts, name))

    async def unreact(self, channel, ts, name):
        self.unreacts.append((ts, name))

    async def reactions(self, channel, ts):
        return self._reactions_script.pop(0) if self._reactions_script else []


class EchoHandler:
    def __init__(self):
        self.approver = DenyAll()

    async def handle_task(self, task: Task) -> Task:
        return task.reply(f"echo: {task.user_text()}")


class EscalatingHandler(EchoHandler):
    async def handle_task(self, task: Task) -> Task:
        return task.reply(
            "human needed", state=TaskState.INPUT_REQUIRED, reason=ReasonCode.ESCALATION
        )


def make(api=None, handler=None, route=None):
    api = api or FakeApi()
    transport = SlackTransport(CH, bot_token="xoxb", app_token="xapp",
                               escalation_route=route, api=api)
    return transport, api, handler or EchoHandler()


def event(text="hi", ts="1.0", thread=None, channel=CH, **extra):
    payload = {"type": "message", "channel": channel, "user": "U1", "text": text, "ts": ts}
    if thread:
        payload["thread_ts"] = thread
    payload.update(extra)
    return payload


async def test_message_gets_reaction_lifecycle_and_threaded_reply():
    transport, api, handler = make()
    await transport.handle_event(event("hello there", ts="100.1"), handler)
    assert ("100.1", "eyes") in api.reacts
    assert ("100.1", "eyes") in api.unreacts
    assert ("100.1", "white_check_mark") in api.reacts
    channel, text, thread_ts = api.posts[-1]
    assert (channel, text, thread_ts) == (CH, "echo: hello there", "100.1")


async def test_duplicate_ts_dispatches_once():
    transport, api, handler = make()
    await transport.handle_event(event(ts="7.7"), handler)
    await transport.handle_event(event(ts="7.7"), handler)
    # app_mention duplicate of the same message dedupes too
    await transport.handle_event(event(ts="7.7", type="app_mention"), handler)
    assert len(api.posts) == 1


async def test_bot_messages_wrong_channel_and_subtypes_are_ignored():
    transport, api, handler = make()
    await transport.handle_event(event(bot_id="B9"), handler)
    await transport.handle_event(event(channel="C0OTHER"), handler)
    await transport.handle_event(event(subtype="message_changed"), handler)
    assert api.posts == []


async def test_thread_continuity_one_task_per_thread():
    transport, api, handler = make()
    await transport.handle_event(event("first", ts="50.0"), handler)
    await transport.handle_event(event("second", ts="51.0", thread="50.0"), handler)
    task = transport._threads["50.0"]
    assert [m.text for m in task.history] == ["first", "echo: first", "second", "echo: second"]
    assert len(transport._threads) == 1


async def test_mention_prefix_is_stripped():
    transport, api, handler = make()
    await transport.handle_event(
        event("<@U0BOT> read the makefile", ts="60.0", type="app_mention"), handler
    )
    assert api.posts[-1][1] == "echo: read the makefile"


async def test_handler_exception_becomes_x_and_error_reply():
    class Exploding(EchoHandler):
        async def handle_task(self, task):
            raise RuntimeError("boom")

    transport, api, handler = make(handler=Exploding())
    await transport.handle_event(event(ts="70.0"), handler)
    assert ("70.0", "x") in api.reacts
    assert "error: boom" in api.posts[-1][1]


async def test_escalation_copies_to_route_channel():
    transport, api, handler = make(handler=EscalatingHandler(), route="slack:C0ESCALATE")
    await transport.handle_event(event("deploy prod", ts="80.0"), handler)
    escalations = [p for p in api.posts if p[0] == "C0ESCALATE"]
    assert len(escalations) == 1
    assert "escalation from task" in escalations[0][1]
    assert ("80.0", "x") in api.reacts  # input-required is not completed


async def test_escalation_skipped_when_route_is_same_channel():
    transport, api, handler = make(handler=EscalatingHandler(), route=f"slack:{CH}")
    await transport.handle_event(event(ts="81.0"), handler)
    assert all(p[2] is not None or p[0] == CH for p in api.posts)
    assert len([p for p in api.posts if p[2] is None]) == 0


async def test_wire_approver_upgrades_denyall_but_respects_explicit():
    transport, api, handler = make()
    transport._wire_approver(handler)
    assert isinstance(handler.approver, SlackReactionApprover)

    class Custom:
        async def approve(self, action, plan):
            return True

    handler2 = EchoHandler()
    handler2.approver = Custom()
    transport._wire_approver(handler2)
    assert isinstance(handler2.approver, Custom)


async def test_reaction_approver_approves_denies_and_times_out():
    api = FakeApi(reactions_script=[[], ["white_check_mark"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01)
    assert await approver.approve("files.write", {"tool": "files.write"}) is True
    assert "approval required" in api.posts[0][1]

    api = FakeApi(reactions_script=[["x"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01)
    assert await approver.approve("files.write", {}) is False

    api = FakeApi()  # nobody ever reacts
    approver = SlackReactionApprover(api, CH, timeout=0.05, poll=0.01)
    assert await approver.approve("files.write", {}) is False  # silence is denial


async def test_deny_beats_approve_when_both_present():
    api = FakeApi(reactions_script=[["white_check_mark", "x"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01)
    assert await approver.approve("files.write", {}) is False


def test_to_mrkdwn_converts_bold_and_headers_but_not_code_fences():
    from daedalus.transports.slack import to_mrkdwn

    text = (
        "### Review\n**install** is fine.\n"
        "```makefile\n** not touched **\n### nor this\n```\n## Done"
    )
    result = to_mrkdwn(text)
    assert "*Review*" in result
    assert "*install* is fine." in result
    assert "** not touched **" in result  # inside the fence, untouched
    assert "### nor this" in result
    assert "*Done*" in result


async def test_replies_are_posted_as_mrkdwn():
    class MarkdownHandler(EchoHandler):
        async def handle_task(self, task):
            return task.reply("### Verdict\n**looks good**")

    transport, api, handler = make(handler=MarkdownHandler())
    await transport.handle_event(event(ts="90.0"), handler)
    assert api.posts[-1][1] == "*Verdict*\n*looks good*"


async def test_dedupe_cache_is_bounded():
    transport, api, handler = make()
    for i in range(600):
        await transport.handle_event(event(f"m{i}", ts=f"{i}.0"), handler)
    assert len(transport._seen) <= 500


def test_serve_without_tokens_is_a_clean_error():
    transport = SlackTransport(CH, bot_token=None, app_token=None)
    transport.bot_token = None  # in case env leaked one in
    transport.app_token = None
    import pytest

    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            transport.serve(EchoHandler())
        )
