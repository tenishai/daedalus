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
import logging
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
_POST_BUDGET = 3900  # Slack renders reliably to ~4000 chars, hard-errors past 40k
log = logging.getLogger(__name__)

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

    async def reactions_with_users(self, channel: str, ts: str) -> list[tuple[str, list[str]]]:
        """Every reaction as (name, [user ids]) — WHO, not only WHAT (#46).

        REPLACES a `reactions()` that returned names and discarded the
        `users` Slack already sends, so at the point an approval decision was
        made the approver's identity was not merely unchecked, it was
        unavailable. That forecloses an allowlist AND leaves no attributable
        record of who authorised a tier-2 action.

        The old method is deleted rather than kept beside this one. I first
        kept it and wrote that it "has other callers"; it had none -- `approve()`
        was the only one, and it calls this instead now. Dead code carrying a
        false reason to exist is worse than no code (#54 r1)."""
        response = await self._web.reactions_get(channel=channel, timestamp=ts)
        message = response.get("message", {}) if hasattr(response, "get") else {}
        return [
            (r.get("name", ""), list(r.get("users", []) or []))
            for r in message.get("reactions", [])
        ]


class SlackReactionApprover:
    """Tier-2 gates in Slack: post the plan, wait for a human reaction.

    WHO APPROVED IS NOW RECORDED, AND OPTIONALLY CHECKED (#46). This said
    "any user reaction counts for now; an approver allowlist is a planned
    hardening before any production use" -- and the agents shipped, so the
    condition that sentence set was passed without the hardening.

    Pass `approvers` (Slack user ids) to require one of them; leave it unset
    and behaviour is exactly as before, any reactor counts. That default is
    deliberate and it is NOT the safe one: refusing every approval on an
    agent whose operator has not yet listed themselves would take tier-2
    tools offline in a running room, unannounced. #53 carries the inversion,
    and the log line below is what makes it answerable -- every decision now
    names its approver, so an operator can read off the real list before
    committing to one.
    """

    APPROVE = {"white_check_mark", "heavy_check_mark", "+1", "thumbsup"}
    DENY = {"x", "no_entry", "-1", "thumbsdown"}

    def __init__(
        self,
        api: SlackApi,
        channel: str,
        *,
        timeout: float = 300.0,
        poll: float = 2.0,
        approvers: frozenset[str] | None = None,
    ):
        self.api = api
        self.channel = channel
        self.timeout = timeout
        self.poll = poll
        self.approvers = approvers
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
            reactions = await self.api.reactions_with_users(self.channel, ts)
            deny = self._who(reactions, self.DENY)
            if deny:
                log.info("tier-2 %s DENIED by %s", action, ", ".join(sorted(deny)))
                return False
            approve = self._who(reactions, self.APPROVE)
            if approve:
                log.info("tier-2 %s APPROVED by %s", action, ", ".join(sorted(approve)))
                return True
            await asyncio.sleep(self.poll)
        log.info("tier-2 %s denied: no reaction within %.0fs", action, self.timeout)
        return False  # silence is denial

    def _who(self, reactions, wanted: set[str]) -> set[str]:
        """The users whose reaction counts, for `wanted`.

        DENY IS CHECKED FIRST BY THE CALLER and that ordering is deliberate,
        but it only settles a tie WITHIN one poll: a tick where both are
        present denies. Across polls the first to appear still wins, which is
        the narrow race #53 also covers."""
        users = {
            user
            for name, reactors in reactions
            if name in wanted
            for user in reactors
        }
        if self.approvers is None:
            return users
        return users & self.approvers


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
