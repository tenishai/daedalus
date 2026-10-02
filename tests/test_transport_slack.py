"""Slack transport logic — the battle scars, replayed as tests.
All against a fake API seam; no slack-sdk needed."""

import asyncio
import logging

from daedalus.protocols import DenyAll
from daedalus.task import ReasonCode, Task, TaskState
from daedalus.transports.slack import (
    SlackApi,
    SlackReactionApprover,
    SlackTransport,
    current_slack_requester,
)

CH = "C0MAIN"


class FakeWeb:
    """Fakes the slack-sdk AsyncWebClient seam one layer below SlackApi, so
    chunking inside SlackApi.post() (which wraps chat_postMessage) is
    actually exercised -- FakeApi above replaces SlackApi wholesale and
    never touches the chunking logic."""

    def __init__(self):
        self.calls: list[dict] = []
        self._ts = 0

    async def chat_postMessage(self, channel, text, thread_ts=None):
        self._ts += 1
        self.calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": f"ts-{self._ts}"}


def _reply_text(api):
    """The exchange's final reply text under the thinking-state lifecycle:
    the placeholder's in-place update when one happened, else the last
    plain post (the fallback paths)."""
    if api.updates:
        return api.updates[-1][2]
    return api.posts[-1][1]


class FakeApi:
    def __init__(self, reactions_script=None):
        self.posts: list[tuple[str, str, str | None]] = []
        self.updates: list[tuple[str, str, str, str | None]] = []
        self.reacts: list[tuple[str, str]] = []
        self.unreacts: list[tuple[str, str]] = []
        self._reactions_script = list(reactions_script or [])
        self._ts = 0
        self.downloaded: list[str] = []
        self.downloads_return: bytes = b""
        self.downloads_raise: Exception | None = None

    async def post(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))
        self._ts += 1
        return f"ts-{self._ts}"

    async def update(self, channel, ts, text, thread_ts=None):
        self.updates.append((channel, ts, text, thread_ts))

    async def react(self, channel, ts, name):
        self.reacts.append((ts, name))

    async def unreact(self, channel, ts, name):
        self.unreacts.append((ts, name))

    async def reactions_with_users(self, channel, ts):
        """The script entries stay plain names; each gets one anonymous
        reactor so existing scripts keep meaning what they meant. A script
        entry may also be a (name, [users]) pair when a test cares WHO."""
        step = self._reactions_script.pop(0) if self._reactions_script else []
        return [item if isinstance(item, tuple) else (item, ["U-someone"]) for item in step]

    async def download_file(self, url):
        self.downloaded.append(url)
        if self.downloads_raise is not None:
            raise self.downloads_raise
        return self.downloads_return


class EchoHandler:
    def __init__(self):
        self.approver = DenyAll()
        self.tasks: list[Task] = []

    async def handle_task(self, task: Task) -> Task:
        self.tasks.append(task)
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
    channel, _pl_ts, text, thread_ts = api.updates[-1]
    assert (channel, text, thread_ts) == (CH, "echo: hello there", "100.1")


async def test_duplicate_ts_dispatches_once():
    transport, api, handler = make()
    await transport.handle_event(event(ts="7.7"), handler)
    await transport.handle_event(event(ts="7.7"), handler)
    # app_mention duplicate of the same message dedupes too
    await transport.handle_event(event(ts="7.7", type="app_mention"), handler)
    assert len(api.posts) == 1  # the placeholder alone; the reply rode its update


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
    task = transport._threads[(CH, "50.0")]
    assert [m.text for m in task.history] == ["first", "echo: first", "second", "echo: second"]
    assert len(transport._threads) == 1


async def test_mention_prefix_is_stripped():
    transport, api, handler = make()
    await transport.handle_event(
        event("<@U0BOT> read the makefile", ts="60.0", type="app_mention"), handler
    )
    assert _reply_text(api) == "echo: read the makefile"


async def test_handler_exception_becomes_x_and_error_reply():
    class Exploding(EchoHandler):
        async def handle_task(self, task):
            raise RuntimeError("boom")

    transport, api, handler = make(handler=Exploding())
    await transport.handle_event(event(ts="70.0"), handler)
    assert ("70.0", "x") in api.reacts
    assert "error: boom" in _reply_text(api)


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


OPS = frozenset({"U-operator"})
ANYONE = frozenset({"U-someone"})  # the reactor FakeApi gives plain-name script entries


async def test_wire_approver_upgrades_denyall_but_respects_explicit():
    api = FakeApi()
    transport = SlackTransport(CH, bot_token="xoxb", app_token="xapp", api=api, approvers=OPS)
    handler = EchoHandler()
    transport._wire_approver(handler)
    assert isinstance(handler.approver, SlackReactionApprover)
    assert handler.approver.approvers == OPS

    class Custom:
        async def approve(self, action, plan):
            return True

    handler2 = EchoHandler()
    handler2.approver = Custom()
    transport._wire_approver(handler2)
    assert isinstance(handler2.approver, Custom)


async def test_reaction_approver_approves_denies_and_times_out():
    api = FakeApi(reactions_script=[[], ["white_check_mark"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=ANYONE)
    assert await approver.approve("files.write", {"tool": "files.write"}) is True
    assert "approval required" in api.posts[0][1]

    api = FakeApi(reactions_script=[["x"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=ANYONE)
    assert await approver.approve("files.write", {}) is False

    api = FakeApi()  # nobody ever reacts
    approver = SlackReactionApprover(api, CH, timeout=0.05, poll=0.01, approvers=ANYONE)
    assert await approver.approve("files.write", {}) is False  # silence is denial


async def test_approval_prompt_is_threaded_with_the_work():
    """Finding #15: the plan posted top-level while replies threaded — the
    conversation read scattered. The transport points the approver at the
    active thread before dispatch."""
    api = FakeApi(reactions_script=[["white_check_mark"]])

    class GatedHandler(EchoHandler):
        def __init__(self, approver):
            self.approver = approver

        async def handle_task(self, task):
            assert await self.approver.approve("files.read", {"tool": "files.read"})
            return task.reply("read it")

    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=ANYONE)
    transport, api2, _ = make(api=api)
    handler = GatedHandler(approver)
    await transport.handle_event(event("read the file", ts="95.0"), handler)
    plan_posts = [p for p in api.posts if "approval required" in p[1]]
    assert plan_posts[0][2] == "95.0"  # threaded under the triggering message


async def test_deny_beats_approve_when_both_present():
    api = FakeApi(reactions_script=[["white_check_mark", "x"]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=ANYONE)
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
    assert _reply_text(api) == "*Verdict*\n*looks good*"


async def test_dedupe_cache_is_bounded():
    transport, api, handler = make()
    for i in range(600):
        await transport.handle_event(event(f"m{i}", ts=f"{i}.0"), handler)
    assert len(transport._seen) <= 500


async def test_tool_blocked_task_gets_x_reaction_and_reply_text():
    """Enforcement blocking a task surfaces through the existing generic
    failed-task handling -- no slack-specific wiring needed."""

    class ToolBlockedHandler(EchoHandler):
        async def handle_task(self, task: Task) -> Task:
            return task.reply(
                "A rule stopped this task: blocked by z.py: nope.",
                state=TaskState.FAILED,
                reason=ReasonCode.TOOL_BLOCKED,
            )

    transport, api, handler = make(handler=ToolBlockedHandler())
    await transport.handle_event(event("deploy", ts="200.0"), handler)
    assert ("200.0", "x") in api.reacts
    assert _reply_text(api) == "A rule stopped this task: blocked by z.py: nope."


def _file(name="pic.png", mimetype="image/png", size=1024, url="https://files.example/pic"):
    return {"name": name, "mimetype": mimetype, "size": size, "url_private": url}


async def test_image_attachment_reaches_the_task_as_base64():
    transport, api, handler = make()
    api.downloads_return = b"hello"
    await transport.handle_event(event("what is this?", ts="200.1", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.text == "what is this?"
    assert message.images is not None
    assert message.images[0].media_type == "image/png"
    assert message.images[0].data == "aGVsbG8="  # base64("hello")
    assert api.downloaded == ["https://files.example/pic"]


async def test_image_only_message_still_reaches_the_agent():
    transport, api, handler = make()
    api.downloads_return = b"x"
    await transport.handle_event(event("", ts="200.2", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.text == "(image)"
    assert message.images is not None


async def test_non_image_file_is_skipped_with_a_note():
    transport, api, handler = make()
    pdf = _file(name="report.pdf", mimetype="application/pdf")
    await transport.handle_event(
        event("see attached", ts="200.3", files=[pdf]),
        handler,
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(attached file report.pdf skipped -- not an image)" in message.text
    assert api.downloaded == []


async def test_oversized_image_is_skipped_before_download():
    transport, api, handler = make()
    await transport.handle_event(
        event("big one", ts="200.4", files=[_file(size=4 * 1024 * 1024)]), handler
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image pic.png skipped -- larger than the 3.5 MB limit)" in message.text
    assert api.downloaded == []


async def test_fifth_image_is_dropped_with_a_count_note():
    transport, api, handler = make()
    api.downloads_return = b"x"
    files = [_file(name=f"p{i}.png", url=f"https://files.example/p{i}") for i in range(5)]
    await transport.handle_event(event("album", ts="200.5", files=files), handler)

    message = handler.tasks[-1].history[0]
    assert message.images is not None and len(message.images) == 4
    assert "(1 additional image(s) skipped -- max 4 per message)" in message.text


async def test_download_failure_skips_that_image_with_a_note():
    transport, api, handler = make()
    api.downloads_raise = RuntimeError("403")
    await transport.handle_event(event("broken", ts="200.6", files=[_file()]), handler)

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image pic.png could not be fetched)" in message.text


async def test_metadata_size_lie_is_caught_after_download():
    """The event's own file metadata is sender-influenced and can lie (or
    be missing) -- the real ceiling is enforced against the bytes actually
    downloaded, not just the claimed size."""
    transport, api, handler = make()
    api.downloads_return = b"x" * (int(3.5 * 1024 * 1024) + 1)
    await transport.handle_event(
        event("big lie", ts="200.61", files=[_file(size=1024)]), handler
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image pic.png skipped -- larger than the 3.5 MB limit)" in message.text


async def test_unsupported_image_mimetype_gets_a_format_note():
    transport, api, handler = make()
    heic = _file(name="photo.heic", mimetype="image/heic")
    await transport.handle_event(event("look", ts="200.62", files=[heic]), handler)

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(image photo.heic skipped -- unsupported image format)" in message.text


async def test_file_with_no_mimetype_could_not_be_read():
    transport, api, handler = make()
    await transport.handle_event(
        event("mystery", ts="200.63", files=[{"id": "F123"}]), handler
    )

    message = handler.tasks[-1].history[0]
    assert message.images is None
    assert "(attached file file could not be read)" in message.text


async def test_download_file_refuses_non_slack_host():
    import pytest

    from daedalus.transports.slack import SlackApi

    api = SlackApi(object())
    with pytest.raises(ValueError, match="non-Slack host"):
        await api.download_file("https://evil.example.com/steal")


async def test_failed_turn_is_rolled_back_so_the_thread_never_bricks():
    """A handler that raises must not leave the rejected message (and any
    images it carried) stuck in history forever -- otherwise every later
    turn re-sends it and fails identically."""

    class RaisesOnce:
        def __init__(self):
            self.approver = DenyAll()
            self.calls = 0

        async def handle_task(self, task):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("rejected")
            task.state = TaskState.COMPLETED
            return task

    handler = RaisesOnce()
    transport, api, _ = make(handler=handler)
    await transport.handle_event(event("bad image", ts="300.1"), handler)
    task = transport._threads[(CH, "300.1")]
    assert task.history == []

    await transport.handle_event(event("plain text", ts="300.2", thread="300.1"), handler)
    task = transport._threads[(CH, "300.1")]
    assert len(task.history) == 1
    assert task.history[0].text == "plain text"


async def test_note_alone_still_forms_a_message():
    transport, api, handler = make()
    await transport.handle_event(
        event("", ts="200.7", files=[_file(name="notes.txt", mimetype="text/plain")]), handler
    )
    # A skipped non-image with no text is still a contentless message... except
    # the skip NOTE is content the sender should see. Ruling: notes alone DO
    # form a message (the agent explains what it could not read), so the task
    # exists and its text is the note.
    message = handler.tasks[-1].history[0]
    assert "(attached file notes.txt skipped -- not an image)" in message.text


async def test_empty_text_and_no_files_is_genuinely_ignored():
    """Unlike the note-alone case above, an event with NO attachments at
    all and no text carries nothing to react to or gather -- this is the
    one truly-empty case, and it must never reach the handler or leave a
    reaction behind."""
    transport, api, handler = make()
    await transport.handle_event(event("", ts="200.71"), handler)
    assert handler.tasks == []
    assert api.reacts == []
    assert api.unreacts == []
    assert api.posts == []


def test_serve_without_tokens_is_a_clean_error():
    transport = SlackTransport(CH, bot_token=None, app_token=None)
    transport.bot_token = None  # in case env leaked one in
    transport.app_token = None
    import pytest

    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            transport.serve(EchoHandler())
        )


# -- long replies get chunked instead of cut off by Slack's render limit --


async def test_post_within_budget_is_a_single_call_unchanged():
    web = FakeWeb()
    api = SlackApi(web)
    ts = await api.post(CH, "short reply", thread_ts="10.0")
    assert len(web.calls) == 1
    assert web.calls[0] == {"channel": CH, "text": "short reply", "thread_ts": "10.0"}
    assert ts == "ts-1"


async def test_long_plain_text_splits_on_paragraph_boundaries_content_preserved():
    from daedalus.transports.slack import _chunk_message

    budget = 3900
    # 6 paragraphs sized so the greedy packer fits exactly two per chunk:
    # three chunks from a message ~2.5x the budget.
    paragraphs = [chr(65 + i) * 1625 for i in range(6)]  # "AAAA...", "BBBB...", ...
    text = "\n\n".join(paragraphs)
    assert len(text) > 2.4 * budget

    chunks = _chunk_message(text, budget=budget)

    assert len(chunks) == 3
    assert "".join(chunks) == text  # no fences involved: no stitching, exact reassembly
    for chunk in chunks:
        assert len(chunk) <= budget
    for paragraph in paragraphs:
        # each paragraph landed whole in exactly one chunk -- never split mid-paragraph
        assert sum(paragraph in chunk for chunk in chunks) == 1


async def test_post_over_budget_makes_multiple_calls_same_thread_first_ts_returned():
    web = FakeWeb()
    api = SlackApi(web)
    paragraphs = [chr(65 + i) * 1625 for i in range(6)]
    text = "\n\n".join(paragraphs)

    ts = await api.post(CH, text, thread_ts="20.0")

    assert len(web.calls) == 3
    assert ts == "ts-1"  # the FIRST posted chunk's ts, not the last
    assert all(call["thread_ts"] == "20.0" for call in web.calls)
    assert all(call["channel"] == CH for call in web.calls)
    assert "".join(call["text"] for call in web.calls) == text


async def test_fence_spanning_chunk_boundary_stays_balanced_and_reassembles():
    from daedalus.transports.slack import _chunk_message

    lines = "\n".join(f"line-{i:02d} of the log output here" for i in range(30))
    text = f"```\n{lines}\n```"

    chunks = _chunk_message(text, budget=120)

    assert len(chunks) > 1
    # every posted chunk renders valid Slack formatting on its own
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0

    # the only real fence delimiters in the source are the outer open/close;
    # every internal boundary was stitched closed-then-reopened by the chunker
    assert chunks[0].startswith("```")
    assert chunks[-1].endswith("```")

    rebuilt = chunks[0]
    for chunk in chunks[1:]:
        assert rebuilt.endswith("\n```")
        rebuilt = rebuilt[: -len("\n```")]
        assert chunk.startswith("```\n")
        rebuilt += chunk[len("```\n") :]
    assert rebuilt == text


async def test_chunked_fenced_message_posts_are_individually_valid_and_threaded():
    web = FakeWeb()
    api = SlackApi(web)
    # long enough to clear the real 3900-char post budget, unlike the
    # small-budget unit test above which only exercises _chunk_message directly
    lines = "\n".join(f"line-{i:04d} of the log output here" for i in range(200))
    text = f"```\n{lines}\n```"

    ts = await api.post(CH, text, thread_ts="30.0")

    assert len(web.calls) > 1
    assert ts == "ts-1"
    for call in web.calls:
        assert call["thread_ts"] == "30.0"
        assert call["text"].count("```") % 2 == 0


async def test_unterminated_source_fence_still_closes_on_the_final_chunk():
    """Chunking reviewer finding #1: an agent that opens a fence and never
    closes it (a long reply, realistic) must not leave the LAST posted
    chunk with a dangling fence -- every chunk renders as valid, balanced
    markdown on its own, even the one that carries the source's own
    unterminated intent."""
    from daedalus.transports.slack import _chunk_message

    text = "```\n" + "a" * 5000
    chunks = _chunk_message(text)

    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


async def test_fenced_chunks_never_exceed_the_post_budget_after_stitching():
    """Chunking reviewer finding #2: stitching a closed/reopened fence onto
    a chunk happens AFTER packing to budget, so a fenced chunk could come
    out over budget. Every chunk actually posted to Slack must stay
    <= the real 3900-char post budget once stitching is done."""
    from daedalus.transports.slack import _chunk_message

    lines = "\n".join(f"line-{i:04d} of the log output here" for i in range(200))
    text = f"```\n{lines}\n```"

    chunks = _chunk_message(text)

    for chunk in chunks:
        assert len(chunk) <= 3900


async def test_fenced_chunks_never_exceed_a_small_post_budget_after_stitching():
    """Same as above at a small budget, where the fixed stitch overhead is
    proportionally much bigger and easiest to see blown."""
    from daedalus.transports.slack import _chunk_message

    lines = "\n".join("LLL" for _ in range(29))
    text = f"```\n{lines}\n```"

    chunks = _chunk_message(text, budget=120)

    for chunk in chunks:
        assert len(chunk) <= 120


def test_fence_marker_straddling_a_hard_split_is_invisible_to_stitching():
    """Disclosed edge, not fixed here: _pack's last-resort hard character
    split cuts purely on character count -- it has no idea "```" is a
    three-character token. If the split lands inside a fence marker on a
    line with no other place to break (one giant unbroken line), neither
    resulting piece contains a whole "```", so _stitch_fences never sees
    the marker at all and the two chunks post with the fence silently
    unbalanced. Pinning today's behavior on purpose rather than engineering
    around a case the reviewer flagged as out of scope for this round."""
    from daedalus.transports.slack import _pack, _stitch_fences

    budget = 20
    text = "x" * (budget - 2) + "```" + "y" * 10  # marker straddles the split

    pieces = _pack(text, budget, (None,))
    chunks = _stitch_fences(pieces)

    assert len(chunks) == 2
    assert "```" not in chunks[0]
    assert "```" not in chunks[1]


# --- thinking states (2026-08-27 operator feedback) ----------------------------


async def _mention(transport, handler, text="hi"):
    await transport.handle_event(
        {"type": "app_mention", "text": text, "ts": "1.0", "channel": CH},
        handler,
    )


def test_thinking_placeholder_posts_then_updates_into_the_reply():
    import asyncio

    from daedalus.transports.slack import SlackTransport

    api = FakeApi()
    transport = SlackTransport(CH, bot_token="x", app_token="y", api=api)
    asyncio.run(_mention(transport, EchoHandler()))

    # The FIRST post of the exchange is the placeholder, in-thread.
    channel, text, thread_ts = api.posts[0]
    assert "thinking" in text
    assert thread_ts == "1.0"
    # The reply arrived as an UPDATE of that same placeholder, not a new post.
    assert len(api.updates) == 1
    _, updated_ts, updated_text, _ = api.updates[0]
    assert updated_ts == "ts-1"
    assert "echo" in updated_text
    # No trailing reply post beyond the placeholder itself.
    assert len(api.posts) == 1  # the placeholder alone; the reply rode its update


def test_thinking_placeholder_failure_falls_back_to_plain_post():
    import asyncio

    from daedalus.transports.slack import SlackTransport

    class NoPlaceholderApi(FakeApi):
        async def post(self, channel, text, thread_ts=None):
            if "thinking" in text:
                raise RuntimeError("slack hiccup")
            return await super().post(channel, text, thread_ts)

    api = NoPlaceholderApi()
    transport = SlackTransport(CH, bot_token="x", app_token="y", api=api)
    asyncio.run(_mention(transport, EchoHandler()))

    assert api.updates == []
    assert any("echo" in text for _, text, _ in api.posts)


def test_update_failure_still_lands_the_reply_as_a_post():
    import asyncio

    from daedalus.transports.slack import SlackTransport

    class NoUpdateApi(FakeApi):
        async def update(self, channel, ts, text, thread_ts=None):
            raise RuntimeError("message too old")

    api = NoUpdateApi()
    transport = SlackTransport(CH, bot_token="x", app_token="y", api=api)
    asyncio.run(_mention(transport, EchoHandler()))

    replies = [text for _, text, _ in api.posts if "thinking" not in text]
    assert any("echo" in text for text in replies)


# ---- shared-channel addressing (two agents, one channel) -------------------
#
# The manifest is tagged-only in channels (2026-08-31 ruling): current apps
# receive only app_mention there. But an app created from an OLDER manifest
# still delivers channel messages -- observed live as two agents in one
# channel both answering a mention aimed at one. The transport now enforces
# the ruling itself: in "channel"/"group", a plain message event is answered
# only when this bot is mentioned; app_mention always passes (Slack delivers
# it only to the mentioned app); "im"/"mpim" keep full delivery. Unknown self
# fails to app_mention-only, so the failure mode is one answer, never two.

SELF_ID = "USELF"
OTHER = "UBOTX"


class AddressingApi(FakeApi):
    async def auth_test(self):
        return SELF_ID


def make_addressed():
    api = AddressingApi()
    transport = SlackTransport(CH, bot_token="xoxb", app_token="xapp", api=api)
    transport.user_id = SELF_ID
    handler = EchoHandler()
    return transport, api, handler


async def test_channel_mention_of_someone_else_is_ignored():
    transport, api, handler = make_addressed()
    await transport.handle_event(
        event(f"<@{OTHER}> what do you do?", channel_type="channel"), handler
    )
    assert api.posts == []


async def test_channel_mention_of_self_is_answered():
    transport, api, handler = make_addressed()
    await transport.handle_event(
        event(f"<@{SELF_ID}> what do you do?", channel_type="channel"), handler
    )
    assert len(api.posts) == 1


async def test_channel_message_with_no_mention_is_ignored():
    """Tagged-only: an unaddressed channel message only ever arrives through
    a stale app manifest, and answering it is how every agent in the channel
    piles on. Silence is the ruling."""
    transport, api, handler = make_addressed()
    await transport.handle_event(event("plain question", channel_type="channel"), handler)
    assert api.posts == []


async def test_channel_mention_of_self_among_others_is_answered():
    transport, api, handler = make_addressed()
    await transport.handle_event(
        event(f"<@{SELF_ID}> <@{OTHER}> both?", channel_type="channel"), handler
    )
    assert len(api.posts) == 1


async def test_private_group_follows_the_same_rule():
    transport, api, handler = make_addressed()
    await transport.handle_event(event("hello?", channel_type="group"), handler)
    assert api.posts == []


async def test_app_mention_always_passes_even_with_unknown_self():
    transport, api, handler = make_addressed()
    transport.user_id = None
    await transport.handle_event(
        event(f"<@{OTHER}> hi", channel_type="channel", type="app_mention"), handler
    )
    assert len(api.posts) == 1


async def test_unknown_self_drops_plain_channel_messages():
    """auth.test failed: a channel message cannot be verified as ours. The
    mentioned agent still answers via its own app_mention -- one answer,
    never two."""
    transport, api, handler = make_addressed()
    transport.user_id = None
    await transport.handle_event(
        event(f"<@{SELF_ID}> hi", channel_type="channel"), handler
    )
    assert api.posts == []


async def test_dm_and_group_dm_keep_full_delivery():
    transport, api, handler = make_addressed()
    await transport.handle_event(event("direct ask", ts="6.1", channel_type="im"), handler)
    await transport.handle_event(event("group ask", ts="6.2", channel_type="mpim"), handler)
    assert len(api.posts) == 2


async def test_events_without_channel_type_keep_the_old_behavior():
    transport, api, handler = make_addressed()
    await transport.handle_event(event("legacy shaped event"), handler)
    assert len(api.posts) == 1


async def test_gated_message_is_not_marked_seen():
    """A gated message must not consume its dedupe slot -- the app_mention
    for the same ts still gets answered."""
    transport, api, handler = make_addressed()
    await transport.handle_event(
        event(f"<@{OTHER}> hi", ts="4.2", channel_type="channel"), handler
    )
    assert api.posts == []
    await transport.handle_event(
        event(f"<@{SELF_ID}> hi", ts="4.2", channel_type="channel", type="app_mention"),
        handler,
    )
    assert len(api.posts) == 1


# --- one transport, many channels (2026-09-04, the second-channel outage) -----

CH2 = "C0SECOND"


def make_multi(api=None, handler=None):
    api = api or FakeApi()
    transport = SlackTransport(
        CH, channels=[CH, CH2], bot_token="xoxb", app_token="xapp", api=api
    )
    transport.user_id = "UBOT"
    return transport, api, handler or EchoHandler()


async def test_second_channel_mention_is_answered_in_its_own_channel():
    transport, api, handler = make_multi()
    await transport.handle_event(
        event("<@UBOT> audit please", ts="20.1", channel=CH2, channel_type="channel"),
        handler,
    )
    # The placeholder, the reply, and both reactions all landed in CH2 --
    # never the primary channel.
    assert api.posts[0][0] == CH2
    assert api.updates[-1][0] == CH2
    assert "echo:" in api.updates[-1][2]


async def test_unlisted_channel_is_still_dropped():
    transport, api, handler = make_multi()
    await transport.handle_event(
        event("<@UBOT> hello", ts="21.1", channel="C0EVIL", channel_type="channel"),
        handler,
    )
    assert not api.posts and not api.reacts


async def test_dm_passes_without_being_listed():
    transport, api, handler = make_multi()
    await transport.handle_event(
        event("hello", ts="22.1", channel="D0DM", channel_type="im"), handler
    )
    assert api.posts[0][0] == "D0DM"
    assert api.updates[-1][0] == "D0DM"


async def test_same_thread_ts_in_two_channels_stay_separate_tasks():
    transport, api, handler = make_multi()
    await transport.handle_event(
        event("<@UBOT> one", ts="30.5", channel=CH, channel_type="channel"), handler
    )
    await transport.handle_event(
        event("<@UBOT> two", ts="30.5", channel=CH2, channel_type="channel"), handler
    )
    # Same ts, different channels: two tasks, two replies, no dedupe collision.
    assert len(api.updates) == 2
    assert {u[0] for u in api.updates} == {CH, CH2}
    assert len(transport._threads) == 2


def test_channels_default_and_dedupe():
    lone = SlackTransport("C1", bot_token="x", app_token="y", api=FakeApi())
    assert lone.channels == ["C1"]
    multi = SlackTransport(
        "C1", channels=["C1", "C1", "C2"], bot_token="x", app_token="y", api=FakeApi()
    )
    assert multi.channels == ["C1", "C2"]


# --- an empty allowlist means every channel the app was invited to -----------
#
# 2026-09-23 operator ruling: asking a human to type channel IDs when they
# must ALSO invite the app to the channel is redundant paperwork. Slack only
# delivers channel events to apps that have been invited, so membership IS
# the permission. An empty allowlist therefore opens the transport to every
# such channel; a non-empty one stays exactly the allowlist it was.

ANY_CH = "C0SOMEWHERE"


def make_open(api=None, handler=None):
    api = api or FakeApi()
    transport = SlackTransport(channels=[], bot_token="xoxb", app_token="xapp", api=api)
    transport.user_id = "UBOT"
    return transport, api, handler or EchoHandler()


async def test_empty_allowlist_answers_in_a_channel_it_never_listed():
    transport, api, handler = make_open()
    await transport.handle_event(
        event("<@UBOT> hello", ts="40.1", channel=ANY_CH, channel_type="channel"),
        handler,
    )
    assert api.posts[0][0] == ANY_CH
    assert "echo:" in api.updates[-1][2]


async def test_empty_allowlist_keeps_the_tagged_only_rule():
    """Open reach is not open season: an unaddressed channel message is
    still not ours to answer."""
    transport, api, handler = make_open()
    await transport.handle_event(
        event("plain question", ts="41.1", channel=ANY_CH, channel_type="channel"),
        handler,
    )
    assert api.posts == [] and api.reacts == []


async def test_empty_allowlist_still_drops_bots_and_subtypes():
    transport, api, handler = make_open()
    await transport.handle_event(
        event("<@UBOT> hi", ts="42.1", channel=ANY_CH, channel_type="channel",
              bot_id="B1"),
        handler,
    )
    await transport.handle_event(
        event("<@UBOT> hi", ts="42.2", channel=ANY_CH, channel_type="channel",
              subtype="message_changed"),
        handler,
    )
    assert api.posts == []


async def test_a_non_empty_allowlist_is_still_an_allowlist():
    """The opt-in behaviour is unchanged for every bundle that names its
    channels -- the new default only applies when nothing is named."""
    transport, api, handler = make_multi()
    await transport.handle_event(
        event("<@UBOT> hello", ts="43.1", channel=ANY_CH, channel_type="channel"),
        handler,
    )
    assert api.posts == []
    await transport.handle_event(
        event("<@UBOT> hello", ts="43.2", channel=CH2, channel_type="channel"), handler
    )
    assert api.posts[0][0] == CH2


async def test_an_approval_with_nowhere_to_ask_is_a_denial_not_a_crash(caplog):
    """An approver built with an empty channel (the public class can be built
    that way; the transport does not wire one on an open transport) has no
    thread to ask in when a tier-2 gate is tripped by a task from ANOTHER
    transport (a2a, cli). Deny and say why; posting to an empty channel id
    would raise out of the agent loop."""
    api = FakeApi()
    approver = SlackReactionApprover(api, "", timeout=0.01, poll=0.001)
    with caplog.at_level(logging.INFO):
        assert await approver.approve("tool.x", {}) is False
    assert api.posts == []
    assert "no slack channel" in caplog.text


async def test_an_open_transport_keeps_tier2_closed(caplog):
    """No channel allowlist means any channel a workspace member invites the app
    to, and the approver follows the latest event's channel. So the DenyAll
    default stays, even with `approvers` configured and even after an event
    arrives from an unlisted channel."""
    transport, api, handler = make_open()
    transport.approvers = OPS
    handler.approver = DenyAll()
    with caplog.at_level(logging.WARNING):
        transport._wire_approver(handler)
    assert isinstance(handler.approver, DenyAll)
    assert "open" in caplog.text
    await transport.handle_event(
        event("<@UBOT> hello", ts="44.1", channel=ANY_CH, channel_type="channel"),
        handler,
    )
    assert isinstance(handler.approver, DenyAll)


async def test_a_listed_transport_still_wires_reaction_approvals():
    api = FakeApi()
    transport = SlackTransport("C1", channels=["C1", "C2"], bot_token="x",
                               app_token="y", api=api, approvers=OPS)
    handler = EchoHandler()
    handler.approver = DenyAll()
    transport._wire_approver(handler)
    assert isinstance(handler.approver, SlackReactionApprover)
    assert handler.approver.channel == "C1"


def test_channels_empty_when_no_channel_is_named():
    open_one = SlackTransport(bot_token="x", app_token="y", api=FakeApi())
    assert open_one.channels == []
    assert open_one.channel == ""
    # An explicit empty list says the same thing, and says it out loud.
    also_open = SlackTransport("", channels=[], bot_token="x", app_token="y",
                               api=FakeApi())
    assert also_open.channels == []


def test_the_startup_line_states_which_reach_is_in_force():
    listed = SlackTransport("C1", channels=["C1", "C2"], bot_token="x", app_token="y",
                            api=FakeApi())
    assert listed.listening_line() == "(slack listening in C1, C2)"
    every = SlackTransport(bot_token="x", app_token="y", api=FakeApi())
    assert every.listening_line() == (
        "(slack listening in every channel the app is invited to)"
    )


def test_runner_builds_an_open_transport_from_a_channelless_block(tmp_path, monkeypatch):
    import sys
    import types

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "transports:\n- kind: slack\n"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"), tools=FakeTools([]))
    slack = [t for t in agent.transports() if isinstance(t, SlackTransport)]
    assert len(slack) == 1
    assert slack[0].channels == []


def test_one_channelless_block_opens_the_whole_transport(tmp_path, monkeypatch):
    """Mixed blocks: naming a channel AND asking for any channel is a request
    for any channel. The named one is still passed as `channel`."""
    import sys
    import types

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "transports:\n- kind: slack\n  channel: C0MAIN\n- kind: slack\n"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"), tools=FakeTools([]))
    slack = [t for t in agent.transports() if isinstance(t, SlackTransport)]
    assert len(slack) == 1
    assert slack[0].channels == []
    assert slack[0].channel == "C0MAIN"


def test_runner_folds_every_slack_block_into_one_transport(tmp_path, monkeypatch):
    import sys
    import types

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "transports:\n"
        "- kind: slack\n  channel: C0MAIN\n"
        "- kind: slack\n  channel: C0SECOND\n"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(
        Bundle.load(tmp_path), llm=ScriptedLLM("done"), tools=FakeTools([])
    )
    transports = agent.transports()
    slack = [t for t in transports if isinstance(t, SlackTransport)]
    assert len(slack) == 1
    assert slack[0].channels == ["C0MAIN", "C0SECOND"]
    assert slack[0].channel == "C0MAIN"  # primary: seeds the approver's channel

# --- markdown tables -> monospace blocks (2026-09-05) -------------------------


def test_table_run_renders_as_aligned_monospace():
    from daedalus.transports.slack import to_mrkdwn

    text = (
        "Here is the summary:\n"
        "| Grant | Ticket | Status |\n"
        "|-------|--------|--------|\n"
        "| **BigQuery** | AREQ-1074 | Compliant |\n"
        "| Product USA team | No ticket | Gap |\n"
        "Done."
    )
    out = to_mrkdwn(text)
    assert "```" in out
    block = out.split("```")[1]
    lines = block.strip().split("\n")
    # Separator row dropped, bold sigils stripped, columns padded to align.
    assert lines[0].startswith("Grant")
    assert "----" not in block
    assert "**" not in block
    assert lines[1].startswith("BigQuery")
    assert lines[0].index("Ticket") == lines[1].index("AREQ-1074")
    assert "Here is the summary:" in out and "Done." in out


def test_long_cells_truncate_with_ellipsis():
    from daedalus.transports.slack import to_mrkdwn

    text = (
        "| Group | Actor |\n"
        "| engineering_operations_search_personalization_global | user@example.com |\n"
    )
    out = to_mrkdwn(text)
    assert "…" in out
    assert "engineering_operations_search_personalization_global" not in out


def test_single_pipe_line_and_fenced_tables_pass_through():
    from daedalus.transports.slack import to_mrkdwn

    lone = "a | b | c"
    assert to_mrkdwn(lone) == lone
    fenced = "```\n| raw | table |\n| kept | as-is |\n```"
    assert to_mrkdwn(fenced) == fenced
async def test_the_approver_allowlist_ignores_a_reaction_from_outside_it():
    """#46: who approved was not merely unchecked, it was UNAVAILABLE.

    `reactions()` returned names and discarded the `users` Slack already
    sends, so at the point of decision there was nothing to check against and
    nothing to record. Anyone in the channel could approve a tier-2 action
    with a thumbs-up, and no trace said who did.
    """
    api = FakeApi(
        reactions_script=[
            [("white_check_mark", ["U-stranger"])],
            [("white_check_mark", ["U-stranger", "U-operator"])],
        ]
    )
    approver = SlackReactionApprover(
        api, CH, timeout=5, poll=0.01, approvers=frozenset({"U-operator"})
    )

    # First poll: only a stranger has reacted — not an approval.
    # Second poll: the operator has, and it is.
    assert await approver.approve("dangerous_tool", {"plan": "x"}) is True
    assert api._reactions_script == []


async def test_an_allowlisted_agent_is_not_approved_by_a_stranger_alone():
    api = FakeApi(reactions_script=[[("white_check_mark", ["U-stranger"])]] * 3)
    approver = SlackReactionApprover(
        api, CH, timeout=0.05, poll=0.01, approvers=frozenset({"U-operator"})
    )

    assert await approver.approve("dangerous_tool", {"plan": "x"}) is False


async def test_with_no_approvers_configured_tier2_is_denied(caplog):
    """#53: absent and empty both fail closed, and nobody is asked to react to
    a plan that cannot be approved. The control (same script, approver listed)
    approves, so the False below means "no approvers" and not "nothing happened"."""
    script = [[("white_check_mark", ["U-anyone"])]]
    control = SlackReactionApprover(
        FakeApi(reactions_script=list(script)), CH, timeout=5, poll=0.01,
        approvers=frozenset({"U-anyone"}),
    )
    assert await control.approve("dangerous_tool", {"plan": "x"}) is True

    for approvers in (None, frozenset()):
        api = FakeApi(reactions_script=list(script))
        approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=approvers)
        with caplog.at_level(logging.WARNING):
            assert await approver.approve("dangerous_tool", {"plan": "x"}) is False
        assert api.posts == []
        assert "no approvers configured" in caplog.text


async def test_the_requester_cannot_approve_their_own_request(caplog):
    """#53: a listed approver who made the request is ignored; a second listed
    approver still counts. Same script with a different requester approves."""
    approvers = frozenset({"U-operator", "U-second"})
    script = [[("white_check_mark", ["U-operator"])]]

    other = SlackReactionApprover(
        FakeApi(reactions_script=list(script)), CH, timeout=5, poll=0.01, approvers=approvers
    )
    current_slack_requester.set("U-second")
    assert await other.approve("deploy", {}) is True

    api = FakeApi(reactions_script=script * 4)
    own = SlackReactionApprover(api, CH, timeout=0.05, poll=0.01, approvers=approvers)
    current_slack_requester.set("U-operator")
    with caplog.at_level(logging.INFO):
        assert await own.approve("deploy", {}) is False
    assert "who made the request" in caplog.text
    assert "APPROVED" not in caplog.text

    api = FakeApi(reactions_script=[[
        ("white_check_mark", ["U-operator"]), ("white_check_mark", ["U-second"])
    ]])
    both = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=approvers)
    current_slack_requester.set("U-operator")
    assert await both.approve("deploy", {}) is True


async def test_a_sole_listed_approver_may_approve_their_own_request(caplog):
    """A solo operator listed alone approves their own request; add a second
    approver and the same reaction is ignored again."""
    script = [[("white_check_mark", ["U-operator"])]]
    solo = SlackReactionApprover(
        FakeApi(reactions_script=list(script)), CH, timeout=5, poll=0.01,
        approvers=frozenset({"U-operator"}),
    )
    current_slack_requester.set("U-operator")
    assert await solo.approve("deploy", {}) is True

    pair = SlackReactionApprover(
        FakeApi(reactions_script=script * 4), CH, timeout=0.05, poll=0.01,
        approvers=frozenset({"U-operator", "U-second"}),
    )
    current_slack_requester.set("U-operator")
    with caplog.at_level(logging.INFO):
        assert await pair.approve("deploy", {}) is False
    assert "who made the request" in caplog.text

    # A different sole approver still cannot be impersonated by the requester.
    other = SlackReactionApprover(
        FakeApi(reactions_script=script * 4), CH, timeout=0.05, poll=0.01,
        approvers=frozenset({"U-second"}),
    )
    current_slack_requester.set("U-operator")
    assert await other.approve("deploy", {}) is False


async def test_the_transport_tells_the_task_who_asked():
    """The requester is the Slack user who sent the message, set per task."""
    api = FakeApi()
    transport = SlackTransport(CH, bot_token="x", app_token="y", api=api, approvers=OPS)
    handler = EchoHandler()
    transport._wire_approver(handler)
    seen = []

    async def handle(task):
        seen.append(current_slack_requester.get())
        return task.reply("ok")

    handler.handle_task = handle
    await transport.handle_event(event("hi", ts="500.1", user="U0ASKER"), handler)
    assert seen == ["U0ASKER"]
    assert current_slack_requester.get() is None  # reset after the task


class GatingHandler(EchoHandler):
    """Runs a tier-2 gate inside handle_task, after an optional pause, and
    records the requester it saw and the gate's verdict."""

    def __init__(self, approver, pause=None):
        super().__init__()
        self.approver = approver
        self.pause = pause
        self.seen: dict[str, tuple] = {}

    async def handle_task(self, task):
        if self.pause is not None:
            await self.pause.wait()
        who = current_slack_requester.get()
        verdict = await self.approver.approve("deploy", {"text": task.user_text()})
        self.seen[task.user_text()] = (who, verdict)
        return task.reply("done")


def _gated_transport(api, approvers, _unused=None):
    transport = SlackTransport(CH, bot_token="x", app_token="y", api=api, approvers=approvers)
    approver = SlackReactionApprover(api, CH, timeout=0.2, poll=0.01, approvers=approvers)
    return transport, approver


async def test_another_user_speaking_mid_task_does_not_change_the_requester():
    """Review r1 #1, scenario 1: Alice (listed, one of several) asks; an
    unlisted account posts before her gate fires; her own tick is still refused."""
    approvers = frozenset({"U0ALICE", "U0CAROL"})
    api = FakeApi(reactions_script=[[("white_check_mark", ["U0ALICE"])]] * 50)
    transport, approver = _gated_transport(api, approvers, None)
    gate = asyncio.Event()
    handler = GatingHandler(approver, pause=gate)
    transport._wire_approver(handler)
    handler.approver = approver
    alice = asyncio.create_task(
        transport.handle_event(event("deploy prod", ts="1.1", user="U0ALICE"), handler)
    )
    await asyncio.sleep(0.01)
    # a different user's event lands while Alice's task is still working
    other = event("hello", ts="1.2", thread="1.2", user="U0ALT")
    await transport.handle_event(other, EchoHandler())
    gate.set()
    await alice
    assert handler.seen["deploy prod"] == ("U0ALICE", False)


async def test_a_gate_outside_a_slack_event_has_no_slack_requester():
    """Review r1 #1, scenario 2: after Bob speaks in Slack, a gate reached from
    another transport sees no requester (not Bob), so nobody is excluded and a
    listed approver, here Bob, is not wrongly special-cased either."""
    approvers = frozenset({"U0ALICE", "U0BOB"})
    api = FakeApi(reactions_script=[[("white_check_mark", ["U0ALICE"])]] * 50)
    transport, approver = _gated_transport(api, approvers, None)
    handler = GatingHandler(approver)
    await transport.handle_event(event("hi", ts="2.1", user="U0BOB"), handler)
    assert handler.seen["hi"][0] == "U0BOB"
    assert current_slack_requester.get() is None
    # a2a-style call: no Slack event around it
    assert await approver.approve("deploy", {}) is True  # Alice's tick counts


async def test_concurrent_tasks_each_see_their_own_requester():
    approvers = frozenset({"U0ALICE", "U0BOB", "U0CAROL"})
    api = FakeApi(reactions_script=[[("white_check_mark", ["U0ALICE"])]] * 50)
    transport, approver = _gated_transport(api, approvers, None)
    gate = asyncio.Event()
    handler = GatingHandler(approver, pause=gate)
    a = asyncio.create_task(
        transport.handle_event(event("from alice", ts="3.1", thread="3.1", user="U0ALICE"), handler)
    )
    b = asyncio.create_task(
        transport.handle_event(event("from bob", ts="3.2", thread="3.2", user="U0BOB"), handler)
    )
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(a, b)
    assert handler.seen["from alice"] == ("U0ALICE", False)  # Alice's own tick refused
    assert handler.seen["from bob"] == ("U0BOB", True)  # Alice may approve Bob's


async def test_a_transport_without_approvers_leaves_tier2_denied(caplog):
    """#53 end to end through the wiring: a channel allowlist alone no longer
    lets any channel member approve."""
    for approvers in (None, []):
        api = FakeApi()
        transport = SlackTransport(
            CH, bot_token="x", app_token="y", api=api, approvers=approvers
        )
        handler = EchoHandler()
        with caplog.at_level(logging.WARNING):
            transport._wire_approver(handler)
        assert isinstance(handler.approver, DenyAll)
        assert "no `approvers`" in caplog.text


async def test_every_decision_names_its_approver_in_the_log(caplog):
    """The log is what makes #53 answerable: an operator can read the real
    approver set off a running agent instead of guessing at one."""
    api = FakeApi(reactions_script=[[("white_check_mark", ["U-operator"])]])
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=OPS)

    with caplog.at_level(logging.INFO):
        assert await approver.approve("deploy", {"plan": "x"}) is True

    assert "U-operator" in caplog.text
    assert "APPROVED" in caplog.text
    assert "deploy" in caplog.text


async def test_a_silent_timeout_is_recorded_as_a_denial_with_its_reason(caplog):
    """`silence is denial` is right, but a requester could not tell a
    deliberate deny from an operator who never saw the message."""
    api = FakeApi(reactions_script=[])
    approver = SlackReactionApprover(api, CH, timeout=0.05, poll=0.01, approvers=OPS)

    with caplog.at_level(logging.INFO):
        assert await approver.approve("deploy", {"plan": "x"}) is False

    assert "no reaction" in caplog.text


async def test_an_OFF_LIST_veto_still_stops_the_action():
    """#54 r1: the allowlist must gate APPROVAL only.

    `_who` applied `users & self.approvers` to whatever set it was handed, and
    `approve()` handed it DENY too — so a stranger's :x: was discarded.
    Measured before the fix, with this exact arrangement: approve() returned
    True, the veto dropped. That is strictly WEAKER than the behaviour this PR
    replaced, where any :x: stopped the action.

    Raising the bar to authorise must never lower the bar to stop.
    """
    approvers = frozenset({"U-operator"})

    # THE POSITIVE CONTROL IS THE TEETH (#54 r2). `is False` alone is
    # `approve()`'s DEFAULT answer, so this test passed against a mutant that
    # simply returned False unconditionally -- it was one of the three new
    # tests, and the only one whose sole assertion was the default. Its two
    # siblings survived that mutant because they assert on log content.
    # Pairing the veto case with the identical script MINUS the veto is what
    # makes the False mean "the :x: did it" rather than "nothing happened".
    without_veto = FakeApi(
        reactions_script=[[("white_check_mark", ["U-operator"])]]
    )
    assert (
        await SlackReactionApprover(
            without_veto, CH, timeout=5, poll=0.01, approvers=approvers
        ).approve("dangerous_tool", {"plan": "x"})
        is True
    ), "the control did not approve, so a False below would prove nothing"

    api = FakeApi(
        reactions_script=[
            [("x", ["U-stranger"]), ("white_check_mark", ["U-operator"])],
        ]
    )
    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01, approvers=approvers)

    assert await approver.approve("dangerous_tool", {"plan": "x"}) is False


async def test_an_off_list_veto_is_named_in_the_log_as_the_denier(caplog):
    api = FakeApi(reactions_script=[[("x", ["U-stranger"])]])
    approver = SlackReactionApprover(
        api, CH, timeout=5, poll=0.01, approvers=frozenset({"U-operator"})
    )

    with caplog.at_level(logging.INFO):
        assert await approver.approve("deploy", {"plan": "x"}) is False

    assert "DENIED" in caplog.text
    assert "U-stranger" in caplog.text


async def test_a_timeout_after_an_OFF_LIST_tick_does_not_claim_there_was_no_reaction(caplog):
    """There was a reaction. Reporting silence puts a false statement into the
    one artefact this class exists to produce (#54 r1)."""
    api = FakeApi(reactions_script=[[("white_check_mark", ["U-stranger"])]] * 4)
    approver = SlackReactionApprover(
        api, CH, timeout=0.05, poll=0.01, approvers=frozenset({"U-operator"})
    )

    with caplog.at_level(logging.INFO):
        assert await approver.approve("deploy", {"plan": "x"}) is False

    assert "off-list" in caplog.text
    assert "no reaction" not in caplog.text


async def test_user_turn_carries_the_slack_sender_id():
    """PR #37 review, issue 2: the learnings lane attributes only to the
    sender the transport recorded, so the transport must record it."""
    transport, api, handler = make()
    await transport.handle_event(event("the gate is manual", ts="400.1", user="U0ALICE"), handler)
    task = transport._threads[(CH, "400.1")]
    assert task.history[0].sender == "U0ALICE"

    await transport.handle_event(event("no user field", ts="400.2", user=None), handler)
    assert transport._threads[(CH, "400.2")].history[0].sender is None


def test_a_named_but_empty_channel_fails_boot(tmp_path, monkeypatch):
    """`channel: ${SLACK_CHANNEL}` with the variable set but empty must not read as
    "every channel": omitting the key opens the transport, an empty value is
    a missing config value."""
    import sys
    import types

    import pytest

    from daedalus.agent import Agent
    from daedalus.bundle import Bundle, BundleError
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    monkeypatch.setenv("SLACK_CHANNEL", "")
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        "transports:\n- kind: slack\n  channel: ${SLACK_CHANNEL}\n"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    agent = Agent(Bundle.load(tmp_path), llm=ScriptedLLM("done"), tools=FakeTools([]))
    with pytest.raises(BundleError, match="value is empty"):
        agent.transports()


def _slack_bundle(tmp_path, block):
    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\n"
        f"transports:\n{block}"
        "tools:\n  provider: none\n  allow: []\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    from daedalus.bundle import Bundle

    return Bundle.load(tmp_path)


def test_bundle_approvers_reach_the_slack_transport_as_a_union(tmp_path, monkeypatch):
    """#53: `approvers` on each slack block is unioned into the one transport."""
    import sys
    import types

    from daedalus.agent import Agent
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    bundle = _slack_bundle(
        tmp_path,
        "- kind: slack\n  channel: C0MAIN\n  approvers: [U0AAA, U0BBB]\n"
        "- kind: slack\n  channel: C0TWO\n  approvers: [U0BBB, U0CCC]\n",
    )
    agent = Agent(bundle, llm=ScriptedLLM("done"), tools=FakeTools([]))
    (slack,) = [t for t in agent.transports() if isinstance(t, SlackTransport)]
    assert slack.approvers == {"U0AAA", "U0BBB", "U0CCC"}


def test_a_slack_block_without_approvers_builds_a_transport_that_denies(tmp_path, monkeypatch):
    import sys
    import types

    from daedalus.agent import Agent
    from tests.fakes import FakeTools, ScriptedLLM

    monkeypatch.setitem(sys.modules, "slack_sdk", types.ModuleType("slack_sdk"))
    bundle = _slack_bundle(tmp_path, "- kind: slack\n  channel: C0MAIN\n")
    agent = Agent(bundle, llm=ScriptedLLM("done"), tools=FakeTools([]))
    (slack,) = [t for t in agent.transports() if isinstance(t, SlackTransport)]
    assert slack.approvers == frozenset()
    handler = EchoHandler()
    slack._api = FakeApi()
    slack._wire_approver(handler)
    assert isinstance(handler.approver, DenyAll)


def test_bundle_validates_the_approvers_field(tmp_path):
    import pytest

    ok = _slack_bundle(
        tmp_path, "- kind: slack\n  channel: C0MAIN\n  approvers: [U0AAA, W0BBB]\n"
    )
    assert ok.spec.transports[0].options()["approvers"] == ["U0AAA", "W0BBB"]
    empty = _slack_bundle(tmp_path, "- kind: slack\n  approvers: []\n")  # legal: same as absent
    assert empty.spec.transports[0].options()["approvers"] == []

    for bad in ("U0AAA", "[U0AAA, '']", "['#ops']", "[u0aaa]", "[123]", "[[U0AAA]]"):
        with pytest.raises(Exception, match="approvers"):
            _slack_bundle(tmp_path, f"- kind: slack\n  approvers: {bad}\n")
    # other transports' extras are untouched
    _slack_bundle(tmp_path, "- kind: a2a\n  approvers: whatever\n")
