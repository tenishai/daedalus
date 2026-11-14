"""slack-actions tool provider -- what an agent in Slack can do there besides
answer: post a message somewhere else, offer buttons or a menu, pin a message,
keep a channel's bookmarks.

A SIDECAR, built whenever the bundle has a `kind: slack` transport block and
stacked beside the primary provider the way slack-read is. Auth is the pod's
own bot token (the env the transport serves with); with no token set it lists
nothing, so the room's dormant Slack wiring costs a turn nothing. The bundle's
`tools.allow` still decides whether a tool here is offered at all (`slack.*`
admits every one), and `approvals.gates` can put any of them behind a person.

Where a tool acts. Every tool takes an optional `channel`; left out, it acts in
the conversation the running task was asked in, which the transport records per
task (`current_slack_conversation`). A task that did not start in Slack (a2a,
cli, a schedule) has no such conversation and must name the channel. When the
bundle's slack blocks name channels, those are the only channels a tool will
act in besides the conversation itself; a bundle that names none defers to
Slack, whose own `not_in_channel` and `channel_not_found` are the refusal.

Never on a colleague's task. The provider does not declare `knowledge_only`, so
a turn another program asked for (an agent over the switchboard, another app's
mention) is not offered these tools: words a program wrote must not be able to
post in a channel under this agent's name.

A message posted somewhere other than the conversation it was asked in ends
with who asked, so nobody can use an agent to speak for them unnamed.

Mirrors slack_read.py's soft-degrade idiom throughout: a missing token, an
unreachable API and every Slack error become plain tool-result text; the
provider never raises out to the agent loop.
"""

from __future__ import annotations

import json
import os
import re

from daedalus.transports.slack import (
    CHOICES_BLOCK_ID,
    ICON_URL_ENV,
    _chunk_message,
    current_slack_conversation,
    current_slack_requester,
    to_mrkdwn,
)

_SLACK_API = "https://slack.com/api"

# Slack's own ceilings, said out loud when a call would cross one.
_BUTTONS_MAX = 5  # past this many choices the message carries a menu
_CHOICES_MAX = 25
_CHOICE_CHARS = 75  # a button's label and a menu option's text
_QUESTION_CHARS = 3000  # a section block's text
_LISTED_MAX = 50
_SNIPPET_CHARS = 200

_PERMALINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
_TS = re.compile(r"^\d{6,}\.\d{1,6}$")
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")

_NO_PLACE = (
    "Tell me which channel: this task did not start in a Slack conversation, "
    "so there is no channel to assume."
)
_NOT_ALLOWED = (
    "That channel is not on my allowed list. I may act in {allowed}, and in the "
    "conversation I was asked in."
)
_NO_TOKEN = "Slack is not connected for this agent, so there is nothing I can do there."

#: Slack's error word -> what it means to the person who asked. Anything not
#: named here is passed on in Slack's own word.
_ERRORS = {
    "not_in_channel": "I am not in that channel. Someone has to add me to it first.",
    "channel_not_found": (
        "Slack does not know that channel, or it is private and I am not in it. "
        "Use the channel's ID, and add me to it if it is private."
    ),
    "is_archived": "That channel is archived.",
    "already_pinned": "That message is already pinned.",
    "no_pin": "That message is not pinned.",
    "message_not_found": "Slack could not find that message in that channel.",
    "not_pinnable": "Slack does not allow that kind of message to be pinned.",
    "too_many_bookmarks": "That channel already has as many bookmarks as Slack allows.",
    "invalid_link": "Slack did not accept that link.",
    "not_found": "Slack could not find that bookmark in that channel.",
}


class _SlackRefused(Exception):
    """Slack answered `ok: false`; the message is the sentence to hand back."""


def _refusal(method: str, data: dict) -> str:
    error = str(data.get("error") or "unknown")
    if error == "missing_scope":
        needed = str(data.get("needed") or "a permission it was not installed with")
        return (
            f"This agent's Slack app is missing `{needed}`. Whoever manages the app "
            "has to update it in Slack before I can do this."
        )
    return _ERRORS.get(error) or f"Slack refused ({method}: {error})."


def _snippet(text: object) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= _SNIPPET_CHARS else flat[: _SNIPPET_CHARS - 3] + "..."


def choices_blocks(question: str, choices: list[str]) -> list[dict]:
    """The question and its choices as Slack blocks: buttons up to five, a menu
    past that. Each element's value IS the choice's words, so the click that
    comes back needs nothing remembered on this side (a restart between the
    offer and the pick loses nothing)."""
    if len(choices) <= _BUTTONS_MAX:
        elements = [
            {
                "type": "button",
                "action_id": f"daedalus_choice_{index}",
                "text": {"type": "plain_text", "text": choice, "emoji": True},
                "value": choice,
            }
            for index, choice in enumerate(choices)
        ]
    else:
        elements = [
            {
                "type": "static_select",
                "action_id": "daedalus_choice",
                "placeholder": {"type": "plain_text", "text": "Choose one"},
                "options": [
                    {"text": {"type": "plain_text", "text": choice, "emoji": True}, "value": choice}
                    for choice in choices
                ],
            }
        ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": question}},
        {"type": "actions", "block_id": CHOICES_BLOCK_ID, "elements": elements},
    ]


class SlackActionsToolProvider:
    def __init__(self, channels: list[str], token_env: str = "SLACK_BOT_TOKEN", client=None):
        # The channels the bundle's slack blocks name; empty means the bundle
        # names none and Slack's own membership rules are the reach.
        self.channels = list(channels)
        self.token_env = token_env
        self._client = client  # injectable: tests pass a fake request client
        self._icon_url = (os.environ.get(ICON_URL_ENV) or "").strip() or None

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        closer = getattr(self._client, "aclose", None)
        if closer is not None:
            await closer()

    def _token(self) -> str | None:
        return os.environ.get(self.token_env, "") or None

    async def tools(self) -> list[dict]:
        if self._token() is None:
            return []
        channel = {
            "type": "string",
            "description": (
                "Channel ID (like C0123ABCD), or a public channel's #name. "
                "Leave out to mean the conversation you were asked in."
            ),
        }
        return [
            {
                "name": "slack.post_message",
                "description": (
                    "Post a message in Slack somewhere other than your reply. Your reply "
                    "already lands in the conversation you were asked in, so never use "
                    "this to answer; use it when someone asks you to put a message in "
                    "another channel, or at the top level of this one. A public channel "
                    "needs no invite; a private one must have you in it. The message "
                    "names who asked for it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The message, in markdown"},
                        "channel": channel,
                        "thread_ts": {
                            "type": "string",
                            "description": "Post inside this thread instead of at the top level",
                        },
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "slack.offer_choices",
                "description": (
                    "Ask a question in Slack with choices people click: buttons for up to "
                    f"{_BUTTONS_MAX} choices, a menu for up to {_CHOICES_MAX}. Use it when "
                    "the next step depends on a pick from a short list. The first person "
                    "to click settles it, and their pick reaches you as their next "
                    "message, so after offering, end your turn with one short line and "
                    "do not guess the answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "What you are asking"},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                f"2 to {_CHOICES_MAX} short choices, each at most "
                                f"{_CHOICE_CHARS} characters"
                            ),
                        },
                        "channel": channel,
                    },
                    "required": ["question", "choices"],
                },
            },
            {
                "name": "slack.pins",
                "description": (
                    "Pin a message to its channel, unpin one, or list a channel's pins. "
                    "`message` says which one: `asked` (the default) is the message that "
                    "asked you, or the one that was reacted to; `thread` is the first "
                    "message of this thread; or give a Slack message link."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "remove", "list"]},
                        "message": {
                            "type": "string",
                            "description": (
                                "`asked`, `thread`, a Slack message link, or the message "
                                "id a post returned"
                            ),
                        },
                        "channel": channel,
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "slack.bookmarks",
                "description": (
                    "Add a link to the bookmarks bar at the top of a channel, remove one, "
                    "or list them. Removing takes the `bookmark_id` that `list` shows."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "remove", "list"]},
                        "title": {"type": "string", "description": "The bookmark's name (add)"},
                        "link": {"type": "string", "description": "The https link (add)"},
                        "bookmark_id": {"type": "string", "description": "Which one (remove)"},
                        "channel": channel,
                    },
                    "required": ["action"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        handlers = {
            "slack.post_message": self._post_message,
            "slack.offer_choices": self._offer_choices,
            "slack.pins": self._pins,
            "slack.bookmarks": self._bookmarks,
        }
        handler = handlers.get(name)
        if handler is None:
            raise LookupError(f"unknown tool {name!r}")
        if self._token() is None:
            return _NO_TOKEN
        try:
            return await handler(payload if isinstance(payload, dict) else {})
        except _SlackRefused as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 -- never raise into the agent loop
            return f"Slack could not be reached ({type(exc).__name__}); nothing was changed."

    # -- where ----------------------------------------------------------------

    def _place(self, channel: object) -> tuple[str | None, str | None]:
        """(channel, None) when the call may go out, (None, refusal) otherwise.
        The conversation the task was asked in always passes: the transport
        admitted it. Any other channel meets the bundle's list when it has one."""
        here = current_slack_conversation.get()
        target = str(channel or "").strip()
        if not target:
            if here is None:
                return None, _NO_PLACE
            return here[0], None
        if here is not None and target == here[0]:
            return target, None
        if self.channels and target not in self.channels:
            return None, _NOT_ALLOWED.format(allowed=", ".join(self.channels))
        return target, None

    def _message(self, which: object, channel: object) -> tuple[str | None, str | None, str | None]:
        """(channel, ts, None) for the message `which` names, or a refusal."""
        here = current_slack_conversation.get()
        word = str(which or "").strip()
        link = _PERMALINK.search(word)
        if link:
            place, refusal = self._place(link.group(1))
            return place, f"{link.group(2)}.{link.group(3)}", refusal
        if _TS.match(word):
            place, refusal = self._place(channel)
            return place, word, refusal
        if word.lower() not in {"", "asked", "thread", "this"}:
            return (
                None,
                None,
                (
                    "Say which message with `asked`, `thread`, or a Slack message link "
                    "(Copy link on the message)."
                ),
            )
        if here is None:
            return (
                None,
                None,
                (
                    "This task did not start in a Slack conversation, so give me a Slack "
                    "message link (Copy link on the message)."
                ),
            )
        return here[0], here[1] if word.lower() == "thread" else here[2], None

    # -- the Web API ------------------------------------------------------------

    async def _slack(self, method: str, payload: dict, *, as_json: bool = False) -> dict:
        """One Slack Web API POST. Raises `_SlackRefused` with the sentence for
        the person on a not-ok answer."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        client = self._ensure_client()
        if as_json:
            response = await client.post(f"{_SLACK_API}/{method}", headers=headers, json=payload)
        else:
            response = await client.post(f"{_SLACK_API}/{method}", headers=headers, data=payload)
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise _SlackRefused(_refusal(method, data if isinstance(data, dict) else {}))
        return data

    async def _send(self, payload: dict) -> dict:
        """chat.postMessage, with the room's picture when the app may carry one
        (the transport's rule: the first refusal drops it for the process)."""
        if self._icon_url:
            try:
                return await self._slack(
                    "chat.postMessage", {**payload, "icon_url": self._icon_url}, as_json=True
                )
            except _SlackRefused:
                self._icon_url = None
        return await self._slack("chat.postMessage", payload, as_json=True)

    # -- the tools ---------------------------------------------------------------

    async def _post_message(self, payload: dict) -> str:
        text = str(payload.get("text") or "").strip()
        if not text:
            return "There is no text to post."
        channel, refusal = self._place(payload.get("channel"))
        if refusal:
            return refusal
        thread = str(payload.get("thread_ts") or "").strip() or None
        rendered = to_mrkdwn(text)
        here = current_slack_conversation.get()
        asker = current_slack_requester.get()
        if asker and not (here and channel == here[0] and thread == here[1]):
            rendered = f"{rendered}\n_Sent for <@{asker}>_"
        first: str | None = None
        for chunk in _chunk_message(rendered):
            body = {"channel": channel, "text": chunk}
            if thread:
                body["thread_ts"] = thread
            sent = await self._send(body)
            first = first or str(sent.get("ts") or "")
            channel = str(sent.get("channel") or channel)
        return f"Posted in {channel} (message {first})."

    async def _offer_choices(self, payload: dict) -> str:
        question = str(payload.get("question") or "").strip()
        raw = payload.get("choices")
        choices = [str(c).strip() for c in raw] if isinstance(raw, list) else []
        choices = list(dict.fromkeys(c for c in choices if c))
        if not question:
            return "There is no question to ask."
        if len(question) > _QUESTION_CHARS:
            return f"Keep the question under {_QUESTION_CHARS} characters."
        if len(choices) < 2:
            return "Give at least two different choices."
        if len(choices) > _CHOICES_MAX:
            return f"Slack shows at most {_CHOICES_MAX} choices here; shorten the list."
        long = [c for c in choices if len(c) > _CHOICE_CHARS]
        if long:
            return (
                f"Each choice must fit in {_CHOICE_CHARS} characters; shorten "
                f"{_snippet(long[0])!r} and offer them again."
            )
        channel, refusal = self._place(payload.get("channel"))
        if refusal:
            return refusal
        here = current_slack_conversation.get()
        body = {
            "channel": channel,
            "text": to_mrkdwn(question),
            "blocks": choices_blocks(to_mrkdwn(question), choices),
        }
        if here is not None and channel == here[0]:
            body["thread_ts"] = here[1]
        await self._send(body)
        return (
            "The choices are posted. The pick will reach you as that person's next "
            "message: end your turn now with one short line, and do not answer for them."
        )

    async def _pins(self, payload: dict) -> str:
        action = str(payload.get("action") or "").strip().lower()
        if action == "list":
            channel, refusal = self._place(payload.get("channel"))
            if refusal:
                return refusal
            data = await self._slack("pins.list", {"channel": channel})
            items = [i for i in data.get("items") or [] if isinstance(i, dict)]
            if not items:
                return f"Nothing is pinned in {channel}."
            lines = [f"Pinned in {channel}:"]
            for item in items[:_LISTED_MAX]:
                message = item.get("message") if isinstance(item.get("message"), dict) else {}
                where = message.get("permalink") or f"message {message.get('ts', '?')}"
                lines.append(f"- {_snippet(message.get('text')) or '(no text)'} ({where})")
            if len(items) > _LISTED_MAX:
                lines.append(f"(and {len(items) - _LISTED_MAX} more not shown)")
            return "\n".join(lines)
        if action not in {"add", "remove"}:
            return "`action` must be add, remove or list."
        channel, ts, refusal = self._message(payload.get("message"), payload.get("channel"))
        if refusal:
            return refusal
        await self._slack(f"pins.{action}", {"channel": channel, "timestamp": ts})
        return f"{'Pinned' if action == 'add' else 'Unpinned'} that message in {channel}."

    async def _bookmarks(self, payload: dict) -> str:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"add", "remove", "list"}:
            return "`action` must be add, remove or list."
        channel, refusal = self._place(payload.get("channel"))
        if refusal:
            return refusal
        if not _CHANNEL_ID.match(channel or ""):
            return "Bookmarks need the channel's ID (like C0123ABCD), not its name."
        if action == "list":
            data = await self._slack("bookmarks.list", {"channel_id": channel})
            marks = [b for b in data.get("bookmarks") or [] if isinstance(b, dict)]
            if not marks:
                return f"{channel} has no bookmarks."
            lines = [f"Bookmarks in {channel}:"]
            lines += [
                f"- {_snippet(b.get('title'))}: {b.get('link') or '(no link)'} "
                f"(bookmark_id {b.get('id')})"
                for b in marks[:_LISTED_MAX]
            ]
            return "\n".join(lines)
        if action == "remove":
            mark = str(payload.get("bookmark_id") or "").strip()
            if not mark:
                return "Say which one with `bookmark_id`; `list` shows them."
            await self._slack("bookmarks.remove", {"channel_id": channel, "bookmark_id": mark})
            return f"Removed that bookmark from {channel}."
        title = str(payload.get("title") or "").strip()
        link = str(payload.get("link") or "").strip()
        if not title or not link.lower().startswith(("https://", "http://")):
            return "A bookmark needs a `title` and a `link` that starts with https://."
        data = await self._slack(
            "bookmarks.add",
            {"channel_id": channel, "title": title[:200], "type": "link", "link": link},
        )
        mark = data.get("bookmark") if isinstance(data.get("bookmark"), dict) else {}
        named = json.dumps(title[:200])
        return f"Bookmarked {named} in {channel} (bookmark_id {mark.get('id', '?')})."
