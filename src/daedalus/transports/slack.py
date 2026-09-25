"""The slack transport — Socket Mode, clean-room.

Carries the battle scars of two prior production Slack agents as
requirements, not code:
- dedupe on (channel, ts): Slack redelivers, and mentions arrive as both
  `message` and `app_mention`
- tagged-only in channels, enforced runtime-side (2026-08-31 ruling: in
  channels, only an explicit @-mention reaches the agent). Any app whose
  manifest subscribes to channel messages -- this repo's
  docs/slack-app-manifest.yaml still does (message.channels,
  message.groups), as do apps created from older manifests -- delivers
  them, and then two agents in one channel both answer a mention aimed
  at one. So the transport enforces the same rule
  itself: in a channel or group, respond only when this bot is mentioned
  (an app_mention event is proof by delivery -- Slack sends it only to the
  mentioned app). DMs and group DMs keep full message delivery.
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
from daedalus.task import ImagePart, Message, ReasonCode, Task, TaskState

_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
_MENTION_ID = re.compile(r"<@([A-Z0-9]+)>")
_SEEN_CAP = 500

logger = logging.getLogger(__name__)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_POST_BUDGET = 3900  # Slack renders reliably to ~4000 chars, hard-errors past 40k
log = logging.getLogger(__name__)

_THINKING = ":hourglass_flowing_sand: _thinking..._"

# Vision bounds. The per-image ceiling is set by the PROVIDER, not Slack:
# Claude via Vertex accepts at most 5 MB base64 per image, which is ~3.75 MB
# raw -- 3.5 MB keeps margin. An image past this is skipped with a note
# (announced, never an opaque provider 400 downstream).
_IMAGE_MAX_BYTES = int(3.5 * 1024 * 1024)
_IMAGE_CAP = 4
_IMAGE_MIMETYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL = re.compile(r"^:?-{2,}:?$")
_CELL_MAX = 26


def _render_table(lines: list[str]) -> str:
    """A run of markdown table rows -> one aligned monospace block. Slack
    has no table rendering at all -- raw pipe rows arrive as soup -- but a
    code fence keeps columns columns on every client. Separator rows are
    dropped, bold sigils inside cells are stripped (they would show
    literally in a fence), and long cells truncate with an ellipsis so a
    wide table stays readable on a narrow pane."""
    rows: list[list[str]] = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(_TABLE_SEP_CELL.fullmatch(c) for c in cells if c):
            continue
        cells = [_BOLD.sub(r"\1", c) for c in cells]
        cells = [
            c if len(c) <= _CELL_MAX else c[: _CELL_MAX - 1] + "\u2026" for c in cells
        ]
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (width - len(r)))
    col = [max(len(r[i]) for r in rows) for i in range(width)]
    rendered = "\n".join(
        "  ".join(r[i].ljust(col[i]) for i in range(width)).rstrip() for r in rows
    )
    return f"```{rendered}```"


def _tables_to_blocks(chunk: str) -> str:
    """Replace each run of two-plus markdown table rows with its rendered
    monospace block; everything else passes through byte-identical."""
    out: list[str] = []
    run: list[str] = []
    for line in chunk.split("\n"):
        if _TABLE_ROW.match(line):
            run.append(line)
            continue
        if len(run) >= 2:
            out.append(_render_table(run))
        else:
            out.extend(run)
        run = []
        out.append(line)
    if len(run) >= 2:
        out.append(_render_table(run))
    else:
        out.extend(run)
    return "\n".join(out)


def to_mrkdwn(text: str) -> str:
    """Light markdown -> Slack mrkdwn: **bold** -> *bold*, headers -> bold
    lines, table runs -> aligned monospace blocks. Fenced code blocks pass
    through untouched (finding #14: models speak markdown; Slack renders
    mrkdwn and shows the raw sigils)."""
    parts = text.split("```")
    for index in range(0, len(parts), 2):  # even indexes are outside fences
        chunk = _tables_to_blocks(parts[index])
        chunk = _BOLD.sub(r"*\1*", chunk)
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

    async def download_file(self, url: str) -> bytes:
        """Fetch a Slack file's bytes. files.url_private requires the bot
        token as a bearer header -- slack_sdk has no download helper, so
        this is a raw authenticated GET (httpx, same as every other
        outbound integration in this repo).

        The bot token is only ever handed to Slack: a payload that lied
        about url_private (or a compromised/misparsed event) must not turn
        this into an SSRF-with-credentials primitive."""
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        if (
            host != "slack-files.com"
            and not host.endswith(".slack-files.com")
            and not host.endswith(".slack.com")
        ):
            raise ValueError("refusing to send the bot token to a non-Slack host")

        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {self._web.token}"}
            )
            response.raise_for_status()
            return response.content

    async def auth_test(self) -> str:
        """This bot's own Slack user id (auth.test), empty string on any
        failure -- the transport treats an unknown self as "skip the
        addressing gate", never as a reason not to serve."""
        try:
            response = await self._web.auth_test()
            return response.get("user_id") or ""
        except Exception:  # noqa: BLE001  identity is an optimization, not a gate
            return ""


class SlackReactionApprover:
    """Tier-2 gates in Slack: post the plan, wait for a human reaction.

    WHO APPROVED IS NOW RECORDED, AND OPTIONALLY CHECKED (#46). This said
    "any user reaction counts for now; an approver allowlist is a planned
    hardening before any production use" -- and the agents shipped, so the
    condition that sentence set was passed without the hardening.

    Pass `approvers` (Slack user ids) to require one of them for APPROVAL --
    a denial is never filtered, because raising the bar to authorise must not
    lower the bar to stop. NOTHING WIRES `approvers` FROM CONFIG YET (#53), so
    in a real deployment this control is presently unreachable: the code below
    is the mechanism, not the hardening. Saying otherwise is how the docstring
    this replaces came to promise a hardening that never arrived.

    Leave it unset and behaviour is exactly as before, any reactor counts.
    That default is
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
        # An approver built with an empty channel has nowhere to ask until a
        # Slack event fills one in, so a gate tripped by a task from ANOTHER
        # transport first would post to "". The transport no longer wires one
        # on an open transport (see _wire_approver), but the approver is
        # public and an explicit caller can still build it that way. Deny,
        # and name the reason: posting to "" would raise out through the
        # agent loop, which is a crash dressed up as a policy decision.
        if not self.channel:
            log.info("tier-2 %s denied: no slack channel to ask in", action)
            return False
        text = (
            f":shield: approval required: `{action}`\n"
            f"```{json.dumps(plan, indent=2, default=str)}```\n"
            "react :white_check_mark: to approve or :x: to deny"
        )
        # in-thread so the plan sits next to the work it gates (finding #15)
        ts = await self.api.post(self.channel, text, thread_ts=self.thread_ts)
        deadline = time.monotonic() + self.timeout
        ignored = False
        while time.monotonic() < deadline:
            reactions = await self.api.reactions_with_users(self.channel, ts)
            # DENY IS NEVER FILTERED BY THE ALLOWLIST, and that asymmetry is the
            # whole point of the list (#54 r1). An allowlist answers "who may
            # AUTHORISE this", and applying it to the veto answers a different
            # question nobody asked: it makes a stranger's :x: DISAPPEAR.
            # Measured before this fix, with approvers={U-operator} and one poll
            # holding a stranger's :x: beside the operator's tick, approve()
            # returned True -- the veto discarded. That is strictly weaker than
            # the behaviour this PR replaced, where any :x: stopped the action.
            # Raising the bar to authorise must never lower the bar to stop.
            deny = self._reactors(reactions, self.DENY)
            if deny:
                log.info("tier-2 %s DENIED by %s", action, ", ".join(sorted(deny)))
                return False
            saw = self._reactors(reactions, self.APPROVE)
            approve = saw if self.approvers is None else saw & self.approvers
            if approve:
                log.info("tier-2 %s APPROVED by %s", action, ", ".join(sorted(approve)))
                return True
            ignored = ignored or bool(saw)
            await asyncio.sleep(self.poll)
        # "NO REACTION" WOULD BE A LIE WHEN THERE WAS ONE. An off-list tick runs
        # the loop to the deadline, and reporting that as silence puts a false
        # statement into the one artefact this class exists to produce.
        if ignored:
            log.info(
                "tier-2 %s denied: only off-list approvals within %.0fs", action, self.timeout
            )
        else:
            log.info("tier-2 %s denied: no reaction within %.0fs", action, self.timeout)
        return False  # silence is denial

    def _reactors(self, reactions, wanted: set[str]) -> set[str]:
        """Every user whose reaction is in `wanted`. UNFILTERED, deliberately —
        the caller decides whether the allowlist applies, and it applies to
        approval only.

        Deny is read before approve, which settles a tie WITHIN one poll: a
        tick holding both denies. Across polls the first to appear still wins,
        which is the narrow race #53 covers."""
        return {
            user
            for name, reactors in reactions
            if name in wanted
            for user in reactors
        }


class SlackTransport:
    def __init__(
        self,
        channel: str = "",
        *,
        channels: list[str] | None = None,
        bot_token: str | None = None,
        app_token: str | None = None,
        escalation_route: str | None = None,
        api: SlackApi | None = None,
    ):
        # `channel` seeds the tier-2 approver before the first event (tier-3
        # escalations follow `escalation_route`, not this);
        # `channels` is the full allowlist this one transport serves. One
        # transport, one socket, many channels -- Socket Mode load-balances
        # each event to exactly ONE open connection per app, so a second
        # transport on the same app token is not a second channel, it is a
        # coin flip that silently loses half the events (2026-09-04, found
        # live the day a second channel was added).
        #
        # AN EMPTY ALLOWLIST IS NOT "NOWHERE", IT IS "EVERYWHERE THE APP WAS
        # INVITED" (2026-09-23 operator ruling). Typing channel IDs into a
        # bundle when a human must ALSO invite the app to the channel is the
        # same permission asked for twice; Slack delivers channel events only
        # to apps that have been invited, so membership already IS the grant.
        # Name channels and this stays an allowlist, exactly as before.
        self.channel = channel
        given = [channel] if channels is None else channels
        self.channels = list(dict.fromkeys(c for c in given if c))
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN")
        self.escalation_route = escalation_route
        self._api = api
        self._seen: OrderedDict[tuple, bool] = OrderedDict()
        self._threads: dict[tuple, Task] = {}  # (channel, thread_ts) -> Task
        # Own Slack user id (auth.test at serve time). None = unknown: the
        # tagged-only gate then trusts only app_mention delivery in channels.
        self.user_id: str | None = None

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
                    "slack: auth.test gave no user id -- channel messages "
                    "cannot be verified as ours and will be answered only "
                    "via app_mention delivery"
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
        print(self.listening_line(), flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await socket.disconnect()

    def listening_line(self) -> str:
        """The one line boot prints about this transport's reach. It states
        which of the two regimes is in force, because "listening in " with
        nothing after it is how an open transport would read as a broken
        one."""
        if not self.channels:
            return "(slack listening in every channel the app is invited to)"
        return f"(slack listening in {', '.join(self.channels)})"

    async def _gather_images(self, files: list[dict]) -> tuple[list[ImagePart], list[str]]:
        """Filter, bound, and fetch the event's image attachments. Every
        skip is a note the sender sees appended to their message text --
        bounds are announced, never silent (wave 3 spec)."""
        import base64

        images: list[ImagePart] = []
        notes: list[str] = []
        skipped_over_cap = 0
        for file in files:
            name = file.get("name") or "file"
            mimetype = file.get("mimetype")
            if mimetype is None:
                notes.append(f"(attached file {name} could not be read)")
                continue
            if mimetype not in _IMAGE_MIMETYPES:
                if mimetype.startswith("image/"):
                    notes.append(f"(image {name} skipped -- unsupported image format)")
                else:
                    notes.append(f"(attached file {name} skipped -- not an image)")
                continue
            if (file.get("size") or 0) > _IMAGE_MAX_BYTES:
                notes.append(f"(image {name} skipped -- larger than the 3.5 MB limit)")
                continue
            if len(images) >= _IMAGE_CAP:
                skipped_over_cap += 1
                continue
            url = file.get("url_private")
            if not url:
                notes.append(f"(image {name} could not be fetched)")
                continue
            try:
                raw = await self._api.download_file(url)
            except Exception as exc:
                # Diagnostics only: never the url (may carry a signed token)
                # or headers (carries the bot token itself).
                print(f"slack image fetch failed for {name}: {type(exc).__name__}")
                notes.append(f"(image {name} could not be fetched)")
                continue
            if len(raw) > _IMAGE_MAX_BYTES:
                # The event's own metadata can lie or be missing entirely --
                # this is the check that actually holds the line.
                notes.append(f"(image {name} skipped -- larger than the 3.5 MB limit)")
                continue
            images.append(
                ImagePart(
                    data=base64.b64encode(raw).decode("ascii"),
                    media_type=mimetype,
                )
            )
        if skipped_over_cap:
            notes.append(
                f"({skipped_over_cap} additional image(s) skipped -- max {_IMAGE_CAP} per message)"
            )
        return images, notes

    def _wire_approver(self, handler: Handler) -> None:
        """Humans are reachable here, so upgrade a fail-closed DenyAll default
        to reaction approvals — an explicitly chosen approver is respected.

        Not on an open transport. The approver follows the channel of the
        latest event, and until #53 wires `approvers` from config, anyone who
        can react there approves. With an allowlist that is a channel the
        bundle named; with none it is any channel a workspace member invites
        the app to, including one they own. So an open transport keeps tier-2
        closed until #53 lands (2026-09-24 operator ruling)."""
        if isinstance(getattr(handler, "approver", None), DenyAll):
            if not self.channels:
                log.warning(
                    "slack transport is open (no channel allowlist): tier-2 "
                    "approvals stay denied until an approver allowlist exists (#53)"
                )
                return
            handler.approver = SlackReactionApprover(self._api, self.channel)

    async def handle_event(self, event: dict, handler: Handler) -> None:
        if event.get("type") not in {"message", "app_mention"}:
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        if (
            self.channels
            and event.get("channel") not in self.channels
            and event.get("channel_type") not in {"im", "mpim"}
        ):
            # Not one of our channels. DMs and group DMs pass regardless --
            # they are full-delivery by design (the tagged-only ruling was
            # about channels), and Slack only delivers them to apps the
            # user deliberately opened a conversation with.
            #
            # No allowlist at all skips this gate entirely: the event only
            # reached us because the app is a member of that channel, and
            # membership is the permission. Every filter below still runs --
            # bots, subtypes, dedupe, and above all the tagged-only rule.
            # It does widen who could approve a tier-2 action, which is why
            # _wire_approver keeps tier-2 closed on an open transport.
            return
        if self._not_for_us_in_channel(event):
            # Tagged-only in channels: a message event that does not mention
            # us is not ours to answer. Checked before the dedupe mark so an
            # app_mention for the same ts is never consumed.
            return
        ts = event.get("ts")
        key = (event.get("channel"), ts)
        if key in self._seen:
            return
        self._seen[key] = True
        while len(self._seen) > _SEEN_CAP:
            self._seen.popitem(last=False)

        text = _MENTION.sub("", event.get("text") or "").strip()
        files = event.get("files") or []
        if not text and not files:
            # Nothing to react to and nothing to gather -- a genuinely
            # empty event, not merely one whose only attachment got
            # filtered out (every filtered attachment leaves a note, so
            # this branch is unreachable once files is non-empty).
            return
        # Everything below answers where the event happened, never the
        # primary channel: one transport serves every allowed channel
        # (plus DMs), so the event's own channel is the only correct
        # destination for reactions, the placeholder, and the reply.
        channel = event.get("channel")
        # React immediately -- before any (possibly slow, sequential) image
        # downloads below -- so the sender sees SOMETHING within
        # milliseconds instead of after every attachment finishes fetching
        # (finding: a 4-image message left the sender staring at silence
        # for as long as the downloads took).
        await self._api.react(channel, ts, "eyes")
        images, notes = await self._gather_images(files)
        if notes:
            text = "\n".join(filter(None, [text, *notes]))
        if not text:
            # An image with no words is still an ask.
            text = "(image)"

        thread = event.get("thread_ts") or ts
        task = self._threads.get((channel, thread))
        if task is None:
            task = Task(context_id=f"slack:{channel}:{thread}")
            self._threads[(channel, thread)] = task
        # Snapshotted so a handler exception below can roll the appended
        # turn back out -- otherwise a rejected/failing message (its image
        # included) stays in history forever and every later turn re-sends
        # it, failing identically each time (thread bricked).
        history_len = len(task.history)
        sender = event.get("user")
        task.history.append(
            Message(
                role="user",
                text=text,
                images=images or None,
                sender=sender if isinstance(sender, str) else None,
            )
        )
        task.state = TaskState.SUBMITTED

        approver = getattr(handler, "approver", None)
        if isinstance(approver, SlackReactionApprover):
            # Point the approver at this event's thread AND channel (the
            # prompt must appear where the human is). Same last-writer-wins
            # behavior under concurrent tasks as thread_ts always had.
            approver.thread_ts = thread
            approver.channel = channel

        # Thinking state (2026-08-27 operator feedback): a visible in-thread
        # placeholder the moment work starts, updated IN PLACE into the
        # final reply below -- the room's Slack presence shows working vs
        # answered as one message's lifecycle, not silence then a wall of
        # text. Placeholder failure is never load-bearing: None falls back
        # to exactly the old post-at-the-end behavior.
        try:
            thinking_ts = await self._api.post(channel, _THINKING, thread_ts=thread)
        except Exception:
            thinking_ts = None
        try:
            task = await handler.handle_task(task)
            self._threads[(channel, thread)] = task
            ok = task.state is TaskState.COMPLETED
            reply = task.agent_text() or "(no reply)"
        except Exception as exc:
            del task.history[history_len:]
            ok = False
            reply = f"error: {exc}"
        await self._api.unreact(channel, ts, "eyes")
        await self._api.react(channel, ts, "white_check_mark" if ok else "x")
        rendered = to_mrkdwn(reply)
        if thinking_ts is not None:
            try:
                await self._api.update(channel, thinking_ts, rendered, thread_ts=thread)
            except Exception:
                # chat.update can fail independently (deleted placeholder,
                # message too old): the reply must still land somewhere.
                await self._api.post(channel, rendered, thread_ts=thread)
        else:
            await self._api.post(channel, rendered, thread_ts=thread)

        if task.reason_code in {ReasonCode.HUMAN_INPUT_REQUIRED, ReasonCode.ESCALATION}:
            await self._escalate(task, channel)

    def _not_for_us_in_channel(self, event: dict) -> bool:
        """The tagged-only rule, enforced runtime-side. True = drop.

        Applies only to plain `message` events in channels and private
        groups (channel_type "channel"/"group"): those reach us only when
        the app's manifest subscribes to message.channels/message.groups
        (docs/slack-app-manifest.yaml does), and answering unaddressed
        ones is how two agents in one channel both reply to a mention
        aimed at one. An `app_mention`
        event always passes -- Slack delivers it only to the mentioned app,
        which is stronger proof than anything we could compute. DMs and
        group DMs ("im"/"mpim") always pass: messaging an agent directly IS
        the ask. When self is unknown (auth.test failed) a channel message
        cannot be verified as ours and is dropped -- the mentioned agent
        still answers through its own app_mention, so the failure mode is
        one correct answer, never two."""
        if event.get("type") == "app_mention":
            return False
        if event.get("channel_type") not in {"channel", "group"}:
            return False
        mentioned = _MENTION_ID.findall(event.get("text") or "")
        return self.user_id is None or self.user_id not in mentioned

    async def _escalate(self, task: Task, channel: str) -> None:
        route = self.escalation_route or ""
        if not route.startswith("slack:"):
            return
        target = route.removeprefix("slack:")
        if target == channel:
            return  # already visible in-thread
        await self._api.post(
            target,
            f":rotating_light: escalation from task `{task.id}` "
            f"in <#{channel}>: {task.agent_text()}",
        )
