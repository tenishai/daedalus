"""The slack transport — Socket Mode, clean-room.

Carries the battle scars of two prior production Slack agents as
requirements, not code:
- dedupe on (channel, ts): Slack redelivers, and mentions arrive as both
  `message` and `app_mention`
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
import os
import re
import time
from collections import OrderedDict

from daedalus.protocols import DenyAll, Handler
from daedalus.task import Message, ReasonCode, Task, TaskState

_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
_SEEN_CAP = 500
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def to_mrkdwn(text: str) -> str:
    """Light markdown -> Slack mrkdwn: **bold** -> *bold*, headers -> bold
    lines. Fenced code blocks pass through untouched (finding #14: models
    speak markdown; Slack renders mrkdwn and shows the raw sigils)."""
    parts = text.split("```")
    for index in range(0, len(parts), 2):  # even indexes are outside fences
        chunk = _BOLD.sub(r"*\1*", parts[index])
        parts[index] = _HEADER.sub(r"*\1*", chunk)
    return "```".join(parts)


class SlackApi:
    """The four Slack operations the transport needs, behind a seam tests fake."""

    def __init__(self, web_client):
        self._web = web_client

    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        response = await self._web.chat_postMessage(
            channel=channel, text=text, thread_ts=thread_ts
        )
        return response["ts"]

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

    async def approve(self, action: str, plan: dict) -> bool:
        text = (
            f":shield: approval required: `{action}`\n"
            f"```{json.dumps(plan, indent=2, default=str)}```\n"
            "react :white_check_mark: to approve or :x: to deny"
        )
        ts = await self.api.post(self.channel, text)
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

        await self._api.react(self.channel, ts, "eyes")
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
        await self._api.post(self.channel, to_mrkdwn(reply), thread_ts=thread)

        if task.reason_code in {ReasonCode.HUMAN_INPUT_REQUIRED, ReasonCode.ESCALATION}:
            await self._escalate(task, thread)

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
