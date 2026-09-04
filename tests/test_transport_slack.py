"""Slack transport logic — the battle scars, replayed as tests.
All against a fake API seam; no slack-sdk needed."""

import asyncio

from daedalus.protocols import DenyAll
from daedalus.task import ReasonCode, Task, TaskState
from daedalus.transports.slack import SlackApi, SlackReactionApprover, SlackTransport

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

    async def reactions(self, channel, ts):
        return self._reactions_script.pop(0) if self._reactions_script else []

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
    task = transport._threads["50.0"]
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

    approver = SlackReactionApprover(api, CH, timeout=5, poll=0.01)
    transport, api2, _ = make(api=api)
    handler = GatedHandler(approver)
    await transport.handle_event(event("read the file", ts="95.0"), handler)
    plan_posts = [p for p in api.posts if "approval required" in p[1]]
    assert plan_posts[0][2] == "95.0"  # threaded under the triggering message


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
    task = transport._threads["300.1"]
    assert task.history == []

    await transport.handle_event(event("plain text", ts="300.2", thread="300.1"), handler)
    task = transport._threads["300.1"]
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
# A mention aimed at a DIFFERENT bot is theirs, not ours. Without the gate,
# every agent in the channel answers every mention -- observed live with two
# agents in one channel. The gate needs to know self (auth.test) and the
# bot-ness of mentioned ids (users.info, cached); both fail OPEN to the old
# channel-inbox behavior, never to silence.

SELF_ID = "USELF"
OTHER_BOT = "UBOTX"
HUMAN = "UHUMAN"


class AddressingApi(FakeApi):
    def __init__(self, bots=(OTHER_BOT,), fail_lookup=False):
        super().__init__()
        self._bots = set(bots)
        self._fail_lookup = fail_lookup
        self.lookups: list[str] = []

    async def auth_test(self):
        return SELF_ID

    async def is_bot_user(self, user_id):
        self.lookups.append(user_id)
        if self._fail_lookup:
            raise RuntimeError("users.info down")
        return user_id in self._bots


def make_addressed(**api_kwargs):
    api = AddressingApi(**api_kwargs)
    transport = SlackTransport(CH, bot_token="xoxb", app_token="xapp", api=api)
    transport.user_id = SELF_ID
    handler = EchoHandler()
    return transport, api, handler


async def test_mention_of_another_bot_is_ignored():
    transport, api, handler = make_addressed()
    await transport.handle_event(event(f"<@{OTHER_BOT}> what do you do?"), handler)
    assert api.posts == []


async def test_mention_of_self_is_answered():
    transport, api, handler = make_addressed()
    await transport.handle_event(event(f"<@{SELF_ID}> what do you do?"), handler)
    assert len(api.posts) == 1


async def test_mention_of_self_alongside_another_bot_is_answered():
    transport, api, handler = make_addressed()
    await transport.handle_event(
        event(f"<@{SELF_ID}> <@{OTHER_BOT}> both of you?"), handler
    )
    assert len(api.posts) == 1


async def test_mention_of_a_human_keeps_the_channel_inbox_behavior():
    transport, api, handler = make_addressed()
    await transport.handle_event(event(f"look at <@{HUMAN}>'s request"), handler)
    assert len(api.posts) == 1


async def test_no_mentions_keeps_the_channel_inbox_behavior():
    transport, api, handler = make_addressed()
    await transport.handle_event(event("plain question"), handler)
    assert len(api.posts) == 1


async def test_unknown_self_disables_the_gate():
    transport, api, handler = make_addressed()
    transport.user_id = None
    await transport.handle_event(event(f"<@{OTHER_BOT}> hi"), handler)
    assert len(api.posts) == 1


async def test_lookup_failure_fails_open_to_answering():
    transport, api, handler = make_addressed(fail_lookup=True)
    await transport.handle_event(event(f"<@{OTHER_BOT}> hi"), handler)
    assert len(api.posts) == 1


async def test_botness_lookups_are_cached():
    transport, api, handler = make_addressed()
    await transport.handle_event(event(f"<@{OTHER_BOT}> one", ts="9.1"), handler)
    await transport.handle_event(event(f"<@{OTHER_BOT}> two", ts="9.2"), handler)
    assert api.lookups == [OTHER_BOT]


async def test_gated_message_is_not_marked_seen():
    """A message gated out must not consume its dedupe slot -- if the same ts
    later arrives as an app_mention naming us (ordering quirks), it still
    gets answered."""
    transport, api, handler = make_addressed()
    await transport.handle_event(event(f"<@{OTHER_BOT}> hi", ts="4.2"), handler)
    assert api.posts == []
    await transport.handle_event(
        event(f"<@{SELF_ID}> hi", ts="4.2", type="app_mention"), handler
    )
    assert len(api.posts) == 1
