"""The slack transport — Socket Mode, clean-room.

Carries the battle scars of two prior production Slack agents as
requirements, not code:
- dedupe on (channel, ts): Slack redelivers, and mentions arrive as both
  `message` and `app_mention`
- addressing in shared channels: a message that mentions OTHER bots and not
  this one is theirs, not ours -- without this, two agents in one channel
  both answer a mention aimed at one of them. Messages with no mention (or
  mentioning humans) keep the channel-inbox behavior.
- one Task per thread: thread_ts is the conversation
- reaction lifecycle: eyes -> white_check_mark / x
- replies stay in-thread
- tier-2 approvals via reactions on a posted plan
- tier-3 escalation (reason 700/704) copied to approvals.escalation.route

Requires the slack extra: pip install 'daedalus[slack]'. Tokens come from
SLACK_BOT_TOKEN and SLACK_APP_TOKEN unless passed explicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict

from daedalus.protocols import DenyAll, Handler
from daedalus.task import Message, ReasonCode, Task, TaskState

_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
_MENTION_ID = re.compile(r"<@([A-Z0-9]+)>")
_SEEN_CAP = 500

logger = logging.getLogger(__name__)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_POST_BUDGET = 3900  # Slack renders reliably to ~4000 chars, hard-errors past 40k
_THINKING = ":hourglass_flowing_sand: _thinking..._"


def to_mrkdwn(text: str) -> str:
    """Light markdown -> Slack mrkdwn: **bold** -> *bold*, headers -> bold
    lines. Fenced code blocks pass through untouched (finding #14: models
    speak markdown; Slack renders mrkdwn and shows the raw sigils)."""
    parts = text.split("```")
    for index in range(0, len(parts), 2):  # even indexes are outside fences
        chunk = _BOLD.sub(r"*\1*", parts[index])
        parts[index] = _HEADER.sub(r"*\1*", chunk)
    return "```".join(parts)


def _split_keep_sep(text: str, sep: str) -> list[str]:
    """Split text on sep, gluing sep back onto the piece before it so
    "".join(pieces) reproduces text exactly."""
    if sep not in text:
        return [text]
    parts = text.split(sep)
    units = [part + sep for part in parts[:-1]]
    if parts[-1]:
        units.append(parts[-1])
    return units


def _pack(text: str, budget: int, seps: tuple[str | None, ...]) -> list[str]:
    """Greedily fill <=budget pieces from text, splitting on seps[0] first
    and falling back to the next separator whenever a single unit is still
    too big to stand alone. seps[-1] must be None, meaning a hard
    character split -- the last-resort base case that always fits."""
    sep, *rest = seps
    if sep is None:
        return [text[i : i + budget] for i in range(0, len(text), budget)]
    chunks: list[str] = []
    current = ""
    for unit in _split_keep_sep(text, sep):
        if len(current) + len(unit) <= budget:
            current += unit
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(unit) <= budget:
            current = unit
        else:
            sub = _pack(unit, budget, tuple(rest))
            chunks.extend(sub[:-1])
            current = sub[-1]
    if current:
        chunks.append(current)
    return chunks


def _stitch_fences(chunks: list[str]) -> list[str]:
    """Keep every chunk independently valid Slack mrkdwn. Fences are
    detected the same way to_mrkdwn does above (``` delimits them, they
    come in pairs): if a fence is still open when a chunk ends, close it
    there and reopen it at the top of the next chunk -- including the
    FINAL chunk. A fence still open after the last chunk means the source
    itself never closed it (an agent's reply cut off mid-fence, realistic
    for long output); closing it there is both what keeps every posted
    chunk balanced and the most faithful rendering of what the source
    meant."""
    stitched: list[str] = []
    in_fence = False
    for chunk in chunks:
        piece = f"```\n{chunk}" if in_fence else chunk
        if chunk.count("```") % 2:
            in_fence = not in_fence
        if in_fence:
            piece = f"{piece}\n```"
        stitched.append(piece)
    return stitched


# Stitching can add a reopen ("```\n") and/or a close ("\n```") to a chunk
# AFTER it was packed to budget -- worst case both, on a chunk that sits
# fully inside an open fence. Reserve that overhead inside the packing
# budget so no posted chunk ever exceeds budget once stitching runs.
_STITCH_OVERHEAD = len("```\n") + len("\n```")


def _chunk_message(text: str, budget: int = _POST_BUDGET) -> list[str]:
    """Split text for chat_postMessage into <=budget pieces: paragraph
    boundaries first, then lines, then a hard character split as a last
    resort. A message within budget comes back as a single-element list,
    unchanged -- today's one-call behavior for the common case."""
    if len(text) <= budget:
        return [text]
    return _stitch_fences(_pack(text, budget - _STITCH_OVERHEAD, ("\n\n", "\n", None)))


class SlackApi:
    """The four Slack operations the transport needs, behind a seam tests fake."""

    def __init__(self, web_client):
        self._web = web_client

    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        """Post text, chunked to stay under Slack's render budget so long
        agent replies stop arriving truncated. Chunks post sequentially
        into the same channel/thread; a failure partway through propagates
        as before, it just leaves fewer chunks posted.

        Returns the FIRST chunk's ts. The one caller that uses this return
        value, SlackReactionApprover.approve(), polls reactions on it to
        gate a tier-2 approval -- returning a single stable ts for "the
        message this call posted" preserves that contract whether the text
        became one message or several.
        """
        chunks = _chunk_message(text)
        first = await self._web.chat_postMessage(
            channel=channel, text=chunks[0], thread_ts=thread_ts
        )
        for chunk in chunks[1:]:
            await self._web.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)
        return first["ts"]

    async def update(
        self, channel: str, ts: str, text: str, thread_ts: str | None = None
    ) -> None:
        """Rewrite an already-posted message in place, chunked the same way
        post() chunks: the first chunk replaces the message via chat.update,
        and any overflow posts as ordinary follow-ups in the same thread.
        The transport uses this to turn its own thinking placeholder INTO
        the reply -- one message that visibly progresses from working to
        answered, instead of a placeholder left behind above the answer."""
        chunks = _chunk_message(text)
        await self._web.chat_update(channel=channel, ts=ts, text=chunks[0])
        for chunk in chunks[1:]:
            await self._web.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)

    async def react(self, channel: str, ts: str, name: str) -> None:
        try:
            await self._web.reactions_add(channel=channel, timestamp=ts, name=name)
        except Exception:
            pass  # reactions are UX, never load-bearing

    async def unreact(self, channel: str, ts: str, name: str) -> None:
        try:
            await self._web.reactions_remove(channel=channel, timestamp=ts, name=name)
        except Exception:
            pass

    async def reactions(self, channel: str, ts: str) -> list[str]:
        response = await self._web.reactions_get(channel=channel, timestamp=ts)
        message = response.get("message", {}) if hasattr(response, "get") else {}
        return [r.get("name", "") for r in message.get("reactions", [])]

    async def auth_test(self) -> str:
        """This bot's own Slack user id (auth.test), empty string on any
        failure -- the transport treats an unknown self as "skip the
        addressing gate", never as a reason not to serve."""
        try:
            response = await self._web.auth_test()
            return response.get("user_id") or ""
        except Exception:  # noqa: BLE001  identity is an optimization, not a gate
            return ""

    async def is_bot_user(self, user_id: str) -> bool:
        """Whether `user_id` is a bot/app user (users.info). Failures read
        as False -- an unresolvable mention must not silence the agent."""
        try:
            response = await self._web.users_info(user=user_id)
            user = response.get("user") or {}
            return bool(user.get("is_bot"))
        except Exception:  # noqa: BLE001  fail-open to the channel-inbox behavior
            return False


class SlackReactionApprover:
    """Tier-2 gates in Slack: post the plan, wait for a human reaction.

    Any user reaction counts for now; an approver allowlist is a planned
    hardening before any production use.
    """

    APPROVE = {"white_check_mark", "heavy_check_mark", "+1", "thumbsup"}
    DENY = {"x", "no_entry", "-1", "thumbsdown"}

    def __init__(self, api: SlackApi, channel: str, *, timeout: float = 300.0, poll: float = 2.0):
        self.api = api
        self.channel = channel
        self.timeout = timeout
        self.poll = poll
        self.thread_ts: str | None = None  # set per-event by the transport

    async def approve(self, action: str, plan: dict) -> bool:
        text = (
            f":shield: approval required: `{action}`\n"
            f"```{json.dumps(plan, indent=2, default=str)}```\n"
            "react :white_check_mark: to approve or :x: to deny"
        )
        # in-thread so the plan sits next to the work it gates (finding #15)
        ts = await self.api.post(self.channel, text, thread_ts=self.thread_ts)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            names = set(await self.api.reactions(self.channel, ts))
            if names & self.DENY:
                return False
            if names & self.APPROVE:
                return True
            await asyncio.sleep(self.poll)
        return False  # silence is denial


class SlackTransport:
    def __init__(
        self,
        channel: str,
        *,
        bot_token: str | None = None,
        app_token: str | None = None,
        escalation_route: str | None = None,
        api: SlackApi | None = None,
    ):
        self.channel = channel
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN")
        self.escalation_route = escalation_route
        self._api = api
        self._seen: OrderedDict[tuple, bool] = OrderedDict()
        self._threads: dict[str, Task] = {}
        # Own Slack user id (auth.test at serve time; None = unknown, the
        # addressing gate skips) and a bot-ness cache for mentioned ids.
        self.user_id: str | None = None
        self._bot_users: dict[str, bool] = {}

    async def serve(self, handler: Handler) -> None:
        if not (self.bot_token and self.app_token):
            raise RuntimeError(
                "slack transport needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN"
            )
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web.async_client import AsyncWebClient

        if self._api is None:
            self._api = SlackApi(AsyncWebClient(token=self.bot_token))
        if self.user_id is None:
            self.user_id = (await self._api.auth_test()) or None
            if self.user_id is None:
                logger.warning(
                    "slack: auth.test gave no user id -- the shared-channel "
                    "addressing gate is disabled; mentions aimed at other "
                    "agents will be answered too"
                )
        self._wire_approver(handler)

        socket = SocketModeClient(
            app_token=self.app_token,
            web_client=AsyncWebClient(token=self.bot_token),
        )

        async def listener(client, request) -> None:
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=request.envelope_id)
            )
            if request.type == "events_api":
                event = request.payload.get("event", {})
                await self.handle_event(event, handler)

        socket.socket_mode_request_listeners.append(listener)
        await socket.connect()
        print(f"(slack listening in {self.channel})")
        try:
            await asyncio.Event().wait()
        finally:
            await socket.disconnect()

    def _wire_approver(self, handler: Handler) -> None:
        """Humans are reachable here, so upgrade a fail-closed DenyAll default
        to reaction approvals — an explicitly chosen approver is respected."""
        if isinstance(getattr(handler, "approver", None), DenyAll):
            handler.approver = SlackReactionApprover(self._api, self.channel)

    async def handle_event(self, event: dict, handler: Handler) -> None:
        if event.get("type") not in {"message", "app_mention"}:
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        if event.get("channel") != self.channel:
            return
        if await self._addressed_to_another_agent(event.get("text") or ""):
            # Shared channel, mention aimed at a different bot: theirs, not
            # ours. Checked before the dedupe mark so nothing is consumed.
            return
        ts = event.get("ts")
        key = (event.get("channel"), ts)
        if key in self._seen:
            return
        self._seen[key] = True
        while len(self._seen) > _SEEN_CAP:
            self._seen.popitem(last=False)

        text = _MENTION.sub("", event.get("text") or "").strip()
        if not text:
            return

        thread = event.get("thread_ts") or ts
        task = self._threads.get(thread)
        if task is None:
            task = Task(context_id=f"slack:{self.channel}:{thread}")
            self._threads[thread] = task
        task.history.append(Message(role="user", text=text))
        task.state = TaskState.SUBMITTED

        approver = getattr(handler, "approver", None)
        if isinstance(approver, SlackReactionApprover):
            approver.thread_ts = thread

        await self._api.react(self.channel, ts, "eyes")
        # Thinking state (2026-08-27 operator feedback): a visible in-thread
        # placeholder the moment work starts, updated IN PLACE into the
        # final reply below -- the room's Slack presence shows working vs
        # answered as one message's lifecycle, not silence then a wall of
        # text. Placeholder failure is never load-bearing: None falls back
        # to exactly the old post-at-the-end behavior.
        try:
            thinking_ts = await self._api.post(self.channel, _THINKING, thread_ts=thread)
        except Exception:
            thinking_ts = None
        try:
            task = await handler.handle_task(task)
            self._threads[thread] = task
            ok = task.state is TaskState.COMPLETED
            reply = task.agent_text() or "(no reply)"
        except Exception as exc:
            ok = False
            reply = f"error: {exc}"
        await self._api.unreact(self.channel, ts, "eyes")
        await self._api.react(self.channel, ts, "white_check_mark" if ok else "x")
        rendered = to_mrkdwn(reply)
        if thinking_ts is not None:
            try:
                await self._api.update(self.channel, thinking_ts, rendered, thread_ts=thread)
            except Exception:
                # chat.update can fail independently (deleted placeholder,
                # message too old): the reply must still land somewhere.
                await self._api.post(self.channel, rendered, thread_ts=thread)
        else:
            await self._api.post(self.channel, rendered, thread_ts=thread)

        if task.reason_code in {ReasonCode.HUMAN_INPUT_REQUIRED, ReasonCode.ESCALATION}:
            await self._escalate(task, thread)

    async def _addressed_to_another_agent(self, text: str) -> bool:
        """True only when the message mentions at least one BOT user and this
        bot is not among the mentions -- the two-agents-one-channel case
        (a mention aimed at a colleague must not draw two answers). Messages
        with no mentions, mentions of humans, or mentions including self keep
        the channel-inbox behavior. Unknown self (auth.test failed) or an
        unresolvable mention fails OPEN to answering, never to silence."""
        if self.user_id is None or self._api is None:
            return False
        mentioned = _MENTION_ID.findall(text)
        if not mentioned or self.user_id in mentioned:
            return False
        for user_id in mentioned:
            if user_id not in self._bot_users:
                try:
                    self._bot_users[user_id] = await self._api.is_bot_user(user_id)
                except Exception:  # noqa: BLE001  fail open, never to silence
                    self._bot_users[user_id] = False
            if self._bot_users[user_id]:
                return True
        return False

    async def _escalate(self, task: Task, thread: str) -> None:
        route = self.escalation_route or ""
        if not route.startswith("slack:"):
            return
        target = route.removeprefix("slack:")
        if target == self.channel:
            return  # already visible in-thread
        await self._api.post(
            target,
            f":rotating_light: escalation from task `{task.id}` "
            f"in <#{self.channel}>: {task.agent_text()}",
        )
