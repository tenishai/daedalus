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

Requires the slack extra: pip install 'tenishai-daedalus[slack]'. Tokens come from
SLACK_BOT_TOKEN and SLACK_APP_TOKEN unless passed explicitly.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Iterable

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

# Slack user id of whoever sent the message that started the running task. Set
# by SlackTransport.handle_event around handler.handle_task, so it is per task
# (each asyncio task has its own context), not per approver. None for any task
# that did not start from a Slack event: a2a, cli, a colleague agent.
current_slack_requester: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_slack_requester", default=None
)

_THINKING = ":hourglass_flowing_sand: _thinking..._"

# How the agent is woken in a channel (2026-10-06, after a user asked an agent
# "do I have to keep mentioning you?" and it answered "no" while its manifest
# said otherwise). The bundle's `kind: slack` block sets `wake:`; the
# deployment can override it with SLACK_WAKE_MODE (the room sets it per agent).
# DMs and group DMs are full delivery in every mode: messaging an agent IS the
# ask. Which mode the manifest must match: `mention` works with app_mention
# alone; `thread` and `channel` need message.channels / message.groups
# subscribed, or Slack never delivers the unmentioned messages at all.
WAKE_MENTION = "mention"  # only an @-mention reaches the agent (the 2026-08-31 ruling)
WAKE_THREAD = "thread"  # mentions, plus every reply in a thread it was asked in at the top
WAKE_CHANNEL = "channel"  # every message in the channel, mentioned or not
WAKE_MODES = frozenset({WAKE_MENTION, WAKE_THREAD, WAKE_CHANNEL})
# Stamped on every Slack-born task's metadata so the prompt can say which
# messages reach the agent (Agent._messages maps it through wake_notice).
SLACK_WAKE_KEY = "slack_wake"
_WAKE_NOTICES = {
    WAKE_MENTION: (
        "You are answering in Slack. In a channel, only a message that @-mentions "
        "you reaches you: a reply in a thread that does not mention you is never "
        "delivered to you. If someone asks whether they must keep mentioning you, "
        "the honest answer is yes, in channels. Direct messages reach you in full."
    ),
    WAKE_THREAD: (
        "You are answering in Slack. In a thread you were @-mentioned in at the top, "
        "every reply reaches you without a mention. A restart forgets those "
        "threads: after one, a reply in an old thread reaches you only if it "
        "@-mentions you, and that does not resume following it; a new thread "
        "started with a mention does. Elsewhere in a channel only a message that "
        "@-mentions you reaches you. Direct messages reach you in full."
    ),
    WAKE_CHANNEL: (
        "You are answering in Slack and you receive every message in this channel, "
        "mentioned or not. Answer what is meant for you; when a message is clearly "
        "between other people, stay out of it."
    ),
}
# Slack's own ceiling on a task_update chunk.
_TASK_UPDATE_MAX = 256
# chat.appendStream takes markdown_text up to 12,000 characters per call.
_STREAM_PIECE = 12000


def wake_notice(mode: object) -> str:
    """The prompt line for a wake mode; "" for anything that is not one.

    Task metadata rides in verbatim from the wire on a2a, so this never turns
    free text into prompt: only one of the three known words maps to a notice."""
    return _WAKE_NOTICES.get(mode, "") if isinstance(mode, str) else ""


# The first line under a streamed message, appended the moment the stream
# opens (2026-10-12): without it a quick turn showed an empty bubble until
# the answer landed, because progress lines only follow runtime events and a
# short turn has none before its answer.
_OPENING = "On it"


def _arg(args: object, *keys: str) -> str:
    """The first of `keys` in a tool's arguments that is a short scalar, as
    text; "" otherwise. Only the keys the caller names are ever read, so a
    query string or a secret never reaches a progress line."""
    if not isinstance(args, dict):
        return ""
    for key in keys:
        value = args.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value):
            # No angle brackets: a model-chosen path like "<!channel>" must
            # never reach Slack as a control sequence.
            text = str(value).replace("<", "").replace(">", "")
            if text:
                return text[:80]
    return ""


# How a tool's name reads as a line. The verb comes from the operation's
# first word, the noun from the rest of it or from the provider, and the
# object from the arguments. Nothing here is per tool: a provider added
# tomorrow reads as "Searching Datadog" or "Reading alerts (Datadog)" with
# no new entry. Only the spellings a plain title-case would get wrong live
# in `_PROVIDER_LABELS`.
_VERBS = {
    "get": "Reading", "read": "Reading", "view": "Reading", "show": "Reading",
    "describe": "Reading", "list": "Listing", "search": "Searching", "query": "Searching",
    "find": "Searching", "grep": "Searching", "glob": "Looking for", "lookup": "Looking up",
    "open": "Opening", "create": "Creating", "add": "Adding", "push": "Pushing",
    "commit": "Saving", "save": "Saving", "store": "Saving", "remember": "Noting",
    "ask": "Asking", "load": "Loading", "recall": "Recalling", "write": "Writing",
    "edit": "Editing", "update": "Updating", "run": "Running", "exec": "Running",
    "bash": "Running", "fetch": "Fetching", "download": "Fetching", "send": "Sending",
    "post": "Posting", "delete": "Removing", "remove": "Removing", "check": "Checking",
    "test": "Testing", "set": "Setting", "sync": "Syncing", "start": "Starting",
    "stop": "Stopping", "poll": "Checking", "wait": "Waiting for", "summarize": "Summarizing",
    "analyze": "Analyzing", "compare": "Comparing", "count": "Counting", "resolve": "Resolving",
    "apply": "Applying", "plan": "Planning", "render": "Rendering", "notify": "Sending",
    "reply": "Replying", "react": "Reacting", "merge": "Merging", "close": "Closing",
    "approve": "Approving", "request": "Requesting", "grant": "Granting", "revoke": "Revoking",
    "scan": "Scanning", "validate": "Validating", "build": "Building", "deploy": "Deploying",
    "restart": "Restarting", "label": "Labelling", "convert": "Converting",
}
_PROVIDER_LABELS = {
    "github": "GitHub", "cloudtrail": "CloudTrail", "jira": "Jira", "atlassian": "Atlassian",
    "slack_read": "Slack", "slack": "Slack", "datadog": "Datadog", "pagerduty": "PagerDuty",
    "colleagues": "", "skills": "skill", "memory": "memory", "claude": "", "web": "the web",
    "files": "files", "scratchpad": "the scratchpad", "notify": "a notification",
}
_NOUNS = {
    "prs": "pull requests", "pr": "pull request", "repo": "repository", "msg": "message",
    "web": "the web",
}
# What a verb acts on when the name says nothing more: `claude.Bash` ->
# "Running a command", `colleagues.remember` -> "Noting that for later".
_DEFAULT_OBJECTS = {"Running": "a command", "Noting": "that for later", "Saving": "to memory"}


def _words(op: str) -> list[str]:
    """`getJiraIssue` / `list_prs` / `Read` -> ["get", "jira", "issue"] ..."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", op).replace("_", " ").replace("-", " ")
    return [w.lower() for w in spaced.split() if w]


def _describe_tool(name: str, args: object) -> str:
    """What a tool call is doing, in a reader's words, built from the tool's
    own name plus the one argument that names its object (a file, a
    repository, a number, a colleague, a key) and nothing else from the
    arguments. Present tense throughout: the event fires as the call starts,
    and a past-tense line under a working agent read as "stuck after it did
    that" (2026-10-12). `github.read_file {path, repository}` ->
    "Reading file teams/data.yaml in example/infra"; `cloudtrail.query` ->
    "Searching CloudTrail"; `colleagues.ask_colleague {name}` -> "Asking
    ledger"; `atlassian.getJiraIssue {key}` -> "Reading jira issue IE-1
    (Atlassian)"."""
    provider, _, op = name.partition(".")
    words = _words(op)
    verb = ""
    if words and words[0] in _VERBS:
        verb, rest = _VERBS[words[0]], words[1:]
    elif len(words) > 1 and words[-1] in _VERBS:
        verb, rest = _VERBS[words[-1]], words[:-1]  # `WebSearch` -> Searching the web
    else:
        rest = words
    obj = _arg(args, "path", "file_path", "number", "pr", "key", "issue", "issue_key", "name",
               "target", "skill", "query_id")
    repo = _arg(args, "repository", "repo")
    rest = [_NOUNS.get(w, w) for w in rest if w not in {"a", "the"}]
    if obj and rest == ["colleague"]:
        rest = []  # "Asking ledger", not "Asking colleague ledger"
    label = _PROVIDER_LABELS.get(provider, provider.replace("_", " ").replace("-", " ").title())
    noun = " ".join(rest)
    if not verb:
        verb = "Using"
        noun = (label + (" " + noun if noun else "")).strip() or "a tool"
    elif not noun and not obj:
        noun = label or _DEFAULT_OBJECTS.get(verb, "")
        if label == "memory" and verb in _DEFAULT_OBJECTS:
            noun = _DEFAULT_OBJECTS[verb]
    line = f"{verb} {noun}".strip()
    if obj and obj != noun:
        line += f" {'#' + obj if obj.isdigit() else obj}"
    if repo and repo != obj:
        line += f" in {repo}"
    if noun != label and label and label not in line and provider not in {
        "colleagues", "claude", "skills", "memory", "github"
    }:
        line += f" ({label})"
    # The tool's NAME is model-chosen too on the hallucinated-tool path
    # (engines/litellm.py emits the raw call name), so the whole line gets
    # the same rule `_arg` applies to argument values.
    return line.replace("<", "").replace(">", "")


def _progress_line(event: dict) -> str | None:
    """One short line of progress for a runtime trace event, or None when the
    event is not worth showing a reader (reasoning text, token counts, the
    engine's own bookkeeping). A tool call reads as what it is doing
    (`_describe_tool`), never as the tool's internal name."""
    kind = event.get("type")
    if kind == "recall":
        count = event.get("count")
        # Fires after the recall, so this is the only line about a finished
        # step; it reads as the thinking that follows it.
        if isinstance(count, int) and count > 0:
            return f"Thinking it over, with {count} thing{'' if count == 1 else 's'} from memory"
        return "Thinking it over"
    if kind == "tool_call":
        name = str(event.get("name") or "")
        return _describe_tool(name, event.get("arguments"))[:_TASK_UPDATE_MAX]
    if kind == "wrap_up":
        return "Writing the answer"
    return None

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

    async def team_id(self) -> str:
        """The workspace id (auth.test `team_id`), "" on any failure. Streaming
        into a channel needs it as `recipient_team_id`; without it the
        transport falls back to the placeholder message, never to silence."""
        try:
            response = await self._web.auth_test()
            return response.get("team_id") or ""
        except Exception:  # noqa: BLE001
            return ""

    # -- streaming (chat.startStream / appendStream / stopStream, slack-sdk >= 3.40) --
    #
    # A streamed message shows progress lines while the agent works and then
    # becomes the answer, in place. The transport treats every one of these as
    # best-effort: a failure to start falls back to the placeholder message
    # path; a failed progress line just stops the progress lines, and the reply
    # still finishes in the stream; only a failure to finish the stream lands
    # the reply as a plain post.

    async def start_stream(
        self, channel: str, thread_ts: str | None, user: str | None, team: str | None
    ) -> str:
        """Open a stream in the thread and return its message ts. Slack
        requires the recipient's user and team ids when the target is a
        channel; in a DM they are accepted and harmless."""
        kwargs: dict = {"channel": channel}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if user:
            kwargs["recipient_user_id"] = user
        if team:
            kwargs["recipient_team_id"] = team
        response = await self._web.chat_startStream(**kwargs)
        return response["ts"]

    async def append_progress(self, channel: str, ts: str, line: str) -> None:
        """One progress line under the streaming message, as italic markdown
        text. Slack's `task_update` chunks were the first choice (2026-10-06),
        and a channel thread showed nothing for them (2026-10-12: an empty
        bubble under the eyes reaction until the answer landed), so a line is
        text the message shows the moment it arrives."""
        # A trailing ellipsis on every line: the newest reads as still in
        # progress, and the ones above it as the steps that led here.
        await self._web.chat_appendStream(
            channel=channel, ts=ts, markdown_text=f"_{line[:_TASK_UPDATE_MAX]}…_\n"
        )

    async def finish_stream(
        self, channel: str, ts: str, markdown: str, mrkdwn: str | None = None
    ) -> None:
        """Append the answer (in pieces under Slack's per-call ceiling) and
        close the stream. Streams take markdown, not mrkdwn, so the caller
        passes the reply as the model wrote it. When `mrkdwn` is given, the
        closed message is then rewritten to the answer alone, so the progress
        lines are what a reader sees while the agent works and the answer is
        what stays; a rewrite Slack refuses leaves the trail above the answer,
        which still reads."""
        await self._web.chat_appendStream(channel=channel, ts=ts, markdown_text="\n")
        for start in range(0, len(markdown), _STREAM_PIECE):
            await self._web.chat_appendStream(
                channel=channel, ts=ts, markdown_text=markdown[start : start + _STREAM_PIECE]
            )
        await self._web.chat_stopStream(channel=channel, ts=ts)
        # ONLY WHEN IT FITS ONE MESSAGE. chat.update takes a single text, and
        # Slack truncates around _POST_BUDGET and refuses past 40k, which is
        # why post() and update() chunk. A longer answer keeps the streamed
        # message, trail and all, which already shows it whole.
        if mrkdwn is not None and len(mrkdwn) <= _POST_BUDGET:
            try:
                await self._web.chat_update(channel=channel, ts=ts, text=mrkdwn)
            except Exception:
                pass


class SlackReactionApprover:
    """Tier-2 gates in Slack: post the plan, wait for a human reaction.

    THE ALLOWLIST IS REQUIRED (#53). `approvers` (Slack user ids, wired from the
    `approvers:` key of the bundle's `kind: slack` block) names who may APPROVE.
    Absent and empty are the same thing: nobody may, so every gate is denied
    before a plan is posted. The earlier default, "unset means any reactor
    approves", was the unsafe one: with a `channel:` allowlist any member of the
    channel, the requester included, approved a tier-2 action with a reaction.

    The requester (`current_slack_requester`, set by the transport for the task
    a Slack message started) does not count as an approver, even when listed,
    so a second person sees the plan. This covers Slack-originated requests
    only: a gate reached from a2a, the cli or a colleague agent has no known
    requester, so nobody is excluded and any listed approver may approve.
    ONE EXCEPTION: when the requester is the only listed approver. A solo
    operator has no second person to ask, and listing only yourself is an
    explicit choice to approve your own actions, the same thing `daedalus chat`
    already does at the terminal. A denial is never filtered, because raising the bar to
    authorise must not lower the bar to stop. Every decision names its Slack
    user id in the log.
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
        if not self.approvers:
            # Fail closed (#53): no allowlist means no one may approve. Checked
            # before posting, so nobody is asked to react to a plan that cannot
            # be approved.
            log.warning(
                "tier-2 %s denied: no approvers configured "
                "(set `approvers: [U...]` on the slack transport block)",
                action,
            )
            return False
        # Per task, via contextvar: a later Slack event cannot change it, and a
        # task not started by a Slack event sees None (no one to exclude).
        requester = current_slack_requester.get()
        text = (
            f":shield: approval required: `{action}`\n"
            f"```{json.dumps(plan, indent=2, default=str)}```\n"
            "react :white_check_mark: to approve or :x: to deny"
        )
        # in-thread so the plan sits next to the work it gates (finding #15)
        ts = await self.api.post(self.channel, text, thread_ts=self.thread_ts)
        deadline = time.monotonic() + self.timeout
        ignored = False
        self_refused = False
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
            # The requester does not approve their own request unless they are
            # the only listed approver (the solo-operator case above).
            approve = saw & self.approvers
            if requester is not None and self.approvers != {requester}:
                approve -= {requester}
            if approve:
                log.info("tier-2 %s APPROVED by %s", action, ", ".join(sorted(approve)))
                return True
            if (
                requester is not None
                and self.approvers != {requester}
                and requester in saw
                and requester in self.approvers
                and not self_refused
            ):
                self_refused = True
                log.info(
                    "tier-2 %s: ignoring approval from %s, who made the request",
                    action,
                    requester,
                )
            ignored = ignored or bool(saw)
            await asyncio.sleep(self.poll)
        # "NO REACTION" WOULD BE A LIE WHEN THERE WAS ONE. An off-list tick runs
        # the loop to the deadline, and reporting that as silence puts a false
        # statement into the one artefact this class exists to produce.
        if ignored:
            log.info(
                "tier-2 %s denied: only off-list or self approvals within %.0fs",
                action,
                self.timeout,
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
        approvers: Iterable[str] | None = None,
        api: SlackApi | None = None,
        wake: str | None = None,
    ):
        # How a channel wakes the agent (WAKE_* above). None means the default,
        # `mention`, which is exactly the behaviour before this option existed.
        self.wake = wake or WAKE_MENTION
        if self.wake not in WAKE_MODES:
            raise ValueError(
                f"slack wake mode {wake!r} is not one of {sorted(WAKE_MODES)}"
            )
        # `approvers` (Slack user ids) are the only people whose reaction approves
        # a tier-2 action (#53); none configured means tier-2 stays denied.
        self.approvers = frozenset(a for a in (approvers or ()) if a)
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
        # Threads this agent was asked in AT THE TOP (the root message mentioned
        # it, or was a DM): in `thread` mode every reply in one of these reaches
        # the agent without a mention. A mention buried in a busy thread answers
        # that one message and adopts nothing. Bounded like `_seen`.
        self._owned: OrderedDict[tuple, bool] = OrderedDict()
        # Workspace id for streaming into channels (auth.test at serve time).
        self.team_id: str | None = None
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
        if self.team_id is None:
            self.team_id = (await self._api.team_id()) or None
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
        wake = {
            WAKE_MENTION: "on @-mention",
            WAKE_THREAD: "on @-mention, then the thread",
            WAKE_CHANNEL: "on every message",
        }[self.wake]
        if not self.channels:
            return f"(slack listening in every channel the app is invited to, {wake})"
        return f"(slack listening in {', '.join(self.channels)}, {wake})"

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
        to reaction approvals, but only when the bundle names who may approve
        (#53). An explicitly chosen approver is respected.

        Tier-2 stays DenyAll, with a warning at boot, in two cases:
        - no `approvers` configured: nobody is entitled to approve;
        - an open transport (no `channel` allowlist, #84): the approver follows
          the channel of the latest event, which on an open transport is any
          channel a workspace member invites the app to, including one they own
          (2026-09-24 operator ruling). Name a `channel` as well to enable tier-2.

        With both, the approver is wired to the first named channel and then
        follows each event's channel. A DM event (`im`/`mpim`, admitted without
        regard to the channel allowlist) retargets it to the DM. In a 1:1 DM
        (`im`) only the requester is present, so a gate there is denied unless
        the requester is the only listed approver. A group DM (`mpim`) can
        include another listed approver, who may approve there.

        `approvers` is transport-wide: unioned across `kind: slack` blocks and
        valid in every channel the approver follows, not scoped per channel."""
        if isinstance(getattr(handler, "approver", None), DenyAll):
            if not self.channels:
                log.warning(
                    "slack transport is open (no channel allowlist): tier-2 "
                    "approvals stay denied; add a `channel` and `approvers` to enable them"
                )
                return
            if not self.approvers:
                log.warning(
                    "slack transport has no `approvers`: tier-2 approvals stay "
                    "denied until the bundle lists who may approve (#53)"
                )
                return
            handler.approver = SlackReactionApprover(
                self._api, self.channel, approvers=self.approvers
            )
            log.info(
                "slack tier-2 approvals enabled; %d approver(s), transport-wide "
                "(unioned across slack blocks, not per channel): %s",
                len(self.approvers),
                ", ".join(sorted(self.approvers)),
            )

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
            # (Documented in docs/design.md 2.4, #87: with DMs enabled on the
            # Slack app, anyone who can DM it bypasses the channel allowlist and
            # only the tool ceiling limits them. The shipped manifest does not
            # subscribe to DM events, so this is opt-in.)
            #
            # No allowlist at all skips this gate entirely: the event only
            # reached us because the app is a member of that channel, and
            # membership is the permission. Every filter below still runs --
            # bots, subtypes, dedupe, and above all the tagged-only rule.
            # It does widen where an approval could be asked for, which is why
            # _wire_approver keeps tier-2 closed on an open transport.
            return
        if self._not_for_us_in_channel(event):
            # The wake rule for channels (mention / thread / channel): a
            # message event this mode does not admit is not ours to answer.
            # Checked before the dedupe mark so an app_mention for the same
            # ts is never consumed.
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
        if thread == ts and self._addressed_to_us(event):
            # The root of a new thread, asked of us: in `thread` mode its
            # replies reach us from here on. `_owned` lives in memory only, so
            # a restart forgets every thread. A mention in an old thread is
            # answered but adopts nothing (it is not a root), so only a new
            # thread started with a mention is followed again. The thread-mode
            # prompt notice says so.
            self._owned[(channel, thread)] = True
            while len(self._owned) > _SEEN_CAP:
                self._owned.popitem(last=False)
        task = self._threads.get((channel, thread))
        if task is None:
            task = Task(context_id=f"slack:{channel}:{thread}")
            self._threads[(channel, thread)] = task
        task.metadata[SLACK_WAKE_KEY] = self.wake
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
            # behavior under concurrent tasks as thread_ts always had. The
            # requester is NOT carried here: see current_slack_requester.
            approver.thread_ts = thread
            approver.channel = channel

        # Working state, in two tiers. Preferred (2026-10-06): a streamed
        # message that shows what the agent is doing as it does it -- each
        # tool call and recall becomes a progress line under one message,
        # which then becomes the answer in place. Fallback (2026-08-27): the
        # hourglass placeholder, updated into the reply. Neither is
        # load-bearing: a stream that will not open falls back to the
        # placeholder, a placeholder that will not post falls back to a
        # plain reply at the end.
        stream_ts: str | None = None
        thinking_ts: str | None = None
        try:
            stream_ts = await self._api.start_stream(
                channel, thread, sender if isinstance(sender, str) else None, self.team_id
            )
        except Exception:
            stream_ts = None
        if stream_ts is None:
            try:
                thinking_ts = await self._api.post(channel, _THINKING, thread_ts=thread)
            except Exception:
                thinking_ts = None
        else:
            # The bubble has words from its first instant; a failure here is
            # the same best-effort as any other progress line.
            try:
                await self._api.append_progress(channel, stream_ts, _OPENING)
            except Exception:
                pass
        # Subscribe to the runtime's trace BEFORE the handler runs: its first
        # events are published synchronously, before a separate task could
        # attach. The pump task then only reads. A handler without an event
        # bus (tests, foreign handlers) streams the answer with only the opening
        # line above it.
        pump_done = asyncio.Event()
        pump = None
        bus = getattr(handler, "events", None)
        if stream_ts is not None and bus is not None and hasattr(bus, "subscribe"):
            _replay, queue = bus.subscribe()  # the replay is other tasks' past; skipped
            pump = asyncio.create_task(
                self._pump_progress(bus, queue, task, channel, stream_ts, pump_done)
            )
        requester_token = current_slack_requester.set(
            sender if isinstance(sender, str) else None
        )
        try:
            task = await handler.handle_task(task)
            self._threads[(channel, thread)] = task
            ok = task.state is TaskState.COMPLETED
            reply = task.agent_text() or "(no reply)"
        except Exception as exc:
            del task.history[history_len:]
            ok = False
            reply = f"error: {exc}"
        finally:
            current_slack_requester.reset(requester_token)
            if pump is not None:
                # Let the pump show the last steps it has queued, then stop it;
                # a pump that will not finish in time is cut, never waited on.
                pump_done.set()
                try:
                    await asyncio.wait_for(pump, 2.0)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    pump.cancel()
        await self._api.unreact(channel, ts, "eyes")
        await self._api.react(channel, ts, "white_check_mark" if ok else "x")
        rendered = to_mrkdwn(reply)
        landed = False
        if stream_ts is not None:
            try:
                # Streams render markdown, so the reply goes as written.
                await self._api.finish_stream(channel, stream_ts, reply, rendered)
                landed = True
            except Exception:
                landed = False  # the plain post below is the reply's safety net
        if not landed and thinking_ts is not None:
            try:
                await self._api.update(channel, thinking_ts, rendered, thread_ts=thread)
                landed = True
            except Exception:
                # chat.update can fail independently (deleted placeholder,
                # message too old): the reply must still land somewhere.
                landed = False
        if not landed:
            await self._api.post(channel, rendered, thread_ts=thread)

        if task.reason_code in {ReasonCode.HUMAN_INPUT_REQUIRED, ReasonCode.ESCALATION}:
            await self._escalate(task, channel)

    def _addressed_to_us(self, event: dict) -> bool:
        """An app_mention, a DM, or a message whose text mentions this bot."""
        if event.get("type") == "app_mention":
            return True
        if event.get("channel_type") not in {"channel", "group"}:
            return True
        mentioned = _MENTION_ID.findall(event.get("text") or "")
        return self.user_id is not None and self.user_id in mentioned

    def _not_for_us_in_channel(self, event: dict) -> bool:
        """The wake rule for channels, enforced runtime-side. True = drop.

        Applies only to plain `message` events in channels and private
        groups (channel_type "channel"/"group"): those reach us only when
        the app's manifest subscribes to message.channels/message.groups,
        and the manifest alone cannot tell "a reply to me" from "a reply to
        anyone" -- which is how an agent once answered every message in a
        busy thread (2026-08-31). An `app_mention` event always passes:
        Slack delivers it only to the mentioned app, which is stronger proof
        than anything we could compute. DMs and group DMs ("im"/"mpim")
        always pass: messaging an agent directly IS the ask.

        - `mention`: only a message that mentions this bot. When self is
          unknown (auth.test failed) a channel message cannot be verified as
          ours and is dropped -- the mentioned agent still answers through
          its own app_mention, so the failure mode is one answer, never two.
        - `thread`: a mention, or a reply in a thread this bot was asked in
          at the top (see `_owned`). A mention deep in someone else's thread
          is answered but adopts nothing.
        - `channel`: nothing is dropped here; every message is the agent's.
        """
        if event.get("type") == "app_mention":
            return False
        if event.get("channel_type") not in {"channel", "group"}:
            return False
        if self.wake == WAKE_CHANNEL:
            return False
        if self._addressed_to_us(event):
            return False
        if self.wake == WAKE_THREAD:
            root = event.get("thread_ts")
            return not (root and (event.get("channel"), root) in self._owned)
        return True

    async def _pump_progress(
        self,
        bus,
        queue: asyncio.Queue,
        task: Task,
        channel: str,
        stream_ts: str,
        done: asyncio.Event,
    ) -> None:
        """Turn this task's runtime trace into progress lines under the
        streaming message until `done` is set and the queue is drained.
        Follows ONLY events stamped with this task's context (EventBus stamps
        them), so two threads working at once never show each other's tool
        calls. A line identical to the one just before it is skipped; the same
        line can still appear again later."""
        mine = task.context_id or task.id
        last: str | None = None
        try:
            while not (done.is_set() and queue.empty()):
                try:
                    event = await asyncio.wait_for(queue.get(), 0.1)
                except TimeoutError:
                    continue
                if event.get("context_id") != mine:
                    continue
                line = _progress_line(event)
                if line is None or line == last:
                    continue
                last = line
                try:
                    await self._api.append_progress(channel, stream_ts, line)
                except Exception:
                    return  # the stream is gone; the reply still lands below
        finally:
            bus.unsubscribe(queue)

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
