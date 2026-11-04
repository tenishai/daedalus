"""slack-read tool provider -- a deployed agent reads conversation threads
from the Slack channels its bundle allows.

A read-only SIDECAR, stacking alongside the primary provider the way
github/cloudtrail already do. The bundle may name the channel IDs the agent
may read; every tool here then refuses any channel outside that list, so the
reach is declared in the bundle, reviewable in the repo, and never widened at
runtime. Auth is the pod's own bot token (the same env the Slack transport
uses) -- membership in a channel is Slack's own second wall: history reads
fail with not_in_channel until a human invites the bot.

AN EMPTY LIST (`channels: []`, written out loud) turns the lane on with NO
allowlist: the agent may read every channel the app has been invited to
(2026-09-23 operator ruling). Membership is then the whole permission, which
is the wall a human already has to build by hand, and Slack's own
`not_in_channel` is the refusal -- surfaced verbatim in the plain sentences
below rather than pre-empted by a membership lookup the tool would have to
keep in step with reality. OMITTING the block entirely still leaves the lane
OFF; absent and empty say different things on purpose.

Why conversations.history scans instead of search.messages: Slack's search
indexer does not reliably hit text inside `<mailto:foo@bar|foo@bar>` link
wrappers -- which is how request bots typically render user emails in a
thread root -- and bot-token search behavior varies by workspace admin
policy. A direct newest-first scan of channel history is slower but
deterministic, and every scan states its budget honestly when it gives up.

Mirrors colleagues.py's soft-degrade idiom throughout: a missing token env,
an unreachable API, and every Slack error all become plain tool-result
text; the provider never raises out to the agent loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import UTC, datetime

_SLACK_API = "https://slack.com/api"

_NO_CHANNEL = (
    "That channel is not on my allowed list. Channels my bundle allows: {allowed}."
)
#: How an empty allowlist describes itself, everywhere it has to. Never
#: "none": an unrestricted lane that reports its reach as nothing is a lie
#: the model would dutifully repeat to the person who asked.
_EVERY_INVITED = "every channel the app is invited to"

# Bounds. Each is a ceiling the tool states out loud when it clips, never a
# silent cut (the github sidecar's honesty rule).
_FIND_DEFAULT = 3
_FIND_CAP = 10
_DAYS_DEFAULT = 90
_DAYS_CAP = 730
_WINDOW_DEFAULT = 7
_WINDOW_CAP = 60
_REPLIES_PER_THREAD = 100
_THREAD_CHARS = 8_000
_PAGE_SIZE = 200
# Threads whose ROOT does not name the person still may: a request bot's root
# often names the requester as <@U...>, and the email only appears in the
# bot's replies (seen 2026-11-04: a root-only scan reported "no thread" for a
# thread that existed). The find scans the replies of up to this many threads
# in the window, a few at a time.
_REPLY_SCAN_CAP = 150
_REPLY_SCAN_CONCURRENCY = 6
_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


def _capped_int(value, default: int, cap: int) -> int:
    """A model-supplied count, capped. Anything int() cannot read ("five",
    a list) reads as the default rather than raising into the agent loop."""
    try:
        return min(int(value or default), cap)
    except (TypeError, ValueError):
        return min(default, cap)


def _fmt_ts(ts: str | float | None) -> str:
    try:
        return datetime.fromtimestamp(float(ts or 0), tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown time"


def _matches(text: str, email: str) -> bool:
    """True when `text` references the person. Matches the full email or --
    when a bot rendered it as a mailto link and Slack normalized the text --
    the bare local-part. Both substrings survive in the raw message text
    Slack returns; the local-part check is cheap insurance."""
    if not text or not email:
        return False
    if email in text:
        return True
    local = email.split("@")[0]
    # A one-or-two-character local part would match half the channel; only
    # fall back to it when it still identifies a person.
    return len(local) >= 3 and local in text


class SlackReadToolProvider:
    def __init__(
        self,
        channels: list[str],
        token_env: str = "SLACK_BOT_TOKEN",
        scan_limit: int = 2000,
        client=None,
        now=time.time,
    ):
        self.channels = list(channels)
        self.token_env = token_env
        self.scan_limit = scan_limit
        self._client = client  # injectable: tests pass a fake request client
        self._now = now
        # Slack user id -> "Name <email>", filled by users.info. None marks a
        # bot that lacks users:read: ids are then shown as they are, once
        # said out loud, instead of failing the read.
        self._people: dict[str, str] | None = {}
        self._by_email: dict[str, str | None] = {}

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _token(self) -> str | None:
        token = os.environ.get(self.token_env, "")
        return token or None

    async def tools(self) -> list[dict]:
        allowed = ", ".join(self.channels) or _EVERY_INVITED
        return [
            {
                "name": "slack.find_threads",
                "description": (
                    "Find threads in an allowed Slack channel that mention "
                    "a person's email address, newest first. Returns each "
                    "matching thread's full text and permalink. When you "
                    "already know WHEN something happened (e.g. from an "
                    "audit-log event), pass `around` -- an anchored window "
                    "is far cheaper and more reliable than scanning back "
                    f"from today. Channels I may read: {allowed}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The person's email address to look for",
                        },
                        "channel": {
                            "type": "string",
                            "description": (
                                "Channel ID to scan (optional when only one "
                                "channel is allowed)"
                            ),
                        },
                        "around": {
                            "type": "string",
                            "description": (
                                'Anchor date (ISO, e.g. "2026-07-10"): search '
                                "only the window around this date instead of "
                                "back from today. Use the event timestamp "
                                "you already hold."
                            ),
                        },
                        "window_days": {
                            "type": "integer",
                            "description": (
                                f"Half-width of the anchored window in days "
                                f"(default {_WINDOW_DEFAULT}, cap {_WINDOW_CAP}; "
                                "only meaningful with `around`)"
                            ),
                        },
                        "days": {
                            "type": "integer",
                            "description": (
                                f"Without `around`: how far back from today "
                                f"(default {_DAYS_DEFAULT}, cap {_DAYS_CAP})"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"At most this many threads (default "
                                f"{_FIND_DEFAULT}, cap {_FIND_CAP})"
                            ),
                        },
                    },
                    "required": ["email"],
                },
            },
            {
                "name": "slack.read_thread",
                "description": (
                    "Read one full thread from an allowed Slack channel by "
                    "its thread timestamp (the ts from slack.find_threads or "
                    "a Slack permalink). Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID"},
                        "thread_ts": {
                            "type": "string",
                            "description": 'The thread timestamp, e.g. "1725400000.123456"',
                        },
                    },
                    "required": ["channel", "thread_ts"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "slack.find_threads":
            return await self._find_threads(
                payload.get("email", "") or "",
                payload.get("channel", "") or "",
                payload.get("days"),
                payload.get("limit"),
                payload.get("around", "") or "",
                payload.get("window_days"),
            )
        if name == "slack.read_thread":
            return await self._read_thread(
                payload.get("channel", "") or "",
                payload.get("thread_ts", "") or "",
            )
        raise LookupError(f"unknown tool {name!r}")

    def _resolve_channel(self, channel: str) -> tuple[str | None, str | None]:
        """(channel_id, None) when the request may proceed, (None, refusal)
        otherwise. A non-empty allowlist is the bundle's declared reach --
        enforced here on every call, not just advertised in the tool
        description. An EMPTY one declares no reach of its own and defers to
        Slack: the call goes out, and `not_in_channel` comes back as the
        refusal if the app was never invited."""
        if not channel:
            if len(self.channels) == 1:
                return self.channels[0], None
            if not self.channels:
                # Nothing to guess from and nothing to list. Saying which
                # channels are allowed would either print "none" (false) or
                # print the workspace (unknowable), so ask for the one fact
                # that settles it.
                return None, (
                    "Tell me which channel ID to read -- I can read "
                    f"{_EVERY_INVITED}, so there is no one channel to assume."
                )
            return None, (
                "More than one channel is allowed; tell me which channel ID "
                f"to read. Channels my bundle allows: {', '.join(self.channels)}."
            )
        if self.channels and channel not in self.channels:
            return None, _NO_CHANNEL.format(allowed=", ".join(self.channels))
        return channel, None

    async def _get(self, method: str, params: dict) -> dict:
        """One Slack Web API GET. Raises RuntimeError with Slack's own error
        string on a not-ok answer so callers can surface not_in_channel /
        missing_scope verbatim instead of a mute failure."""
        token = self._token()
        if token is None:
            raise RuntimeError(f"no token in ${self.token_env}")
        client = self._ensure_client()
        response = await client.get(
            f"{_SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error', 'unknown')}")
        return data

    async def _person(self, user_id: str) -> str:
        """"Name <email> (U123)" when users:read lets the bot look the id
        up, else the bare id. Cached for the process."""
        if not user_id:
            return "?"
        if self._people is None:
            return user_id
        if user_id in self._people:
            return self._people[user_id]
        try:
            data = await self._get("users.info", {"user": user_id})
        except RuntimeError as exc:
            if "missing_scope" in str(exc) or "not_allowed" in str(exc):
                self._people = None
                return user_id
            self._people[user_id] = user_id
            return user_id
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        name = profile.get("real_name") or user.get("real_name") or user.get("name") or ""
        email = profile.get("email") or ""
        if email:
            label = f"{name} <{email}> ({user_id})"
        else:
            label = f"{name} ({user_id})" if name else user_id
        self._people[user_id] = label
        return label

    async def _user_id_for(self, email: str) -> str | None:
        """The Slack id for an email (users.lookupByEmail, users:read.email),
        or None when the scope is missing or nobody has that address."""
        if email in self._by_email:
            return self._by_email[email]
        try:
            data = await self._get("users.lookupByEmail", {"email": email})
            uid = (data.get("user") or {}).get("id")
        except RuntimeError:
            uid = None
        self._by_email[email] = uid
        return uid

    async def _render(self, m: dict) -> str:
        """One message as text, with who wrote it, every mention resolved
        where the scope allows, and its reactions with who reacted -- the
        approval evidence (a :+1: from an approver) lives there."""
        text = m.get("text") or ""
        ids = sorted(set(_MENTION.findall(text)))
        for uid in ids:
            label = await self._person(uid)
            if label != uid:
                text = re.sub(rf"<@{uid}(?:\|[^>]*)?>", f"@{label}", text)
        if m.get("user"):
            author = await self._person(m["user"])
        else:
            bot = (m.get("bot_profile") or {}).get("name")
            author = "bot:" + (bot or m.get("username") or m.get("bot_id") or "?")
        line = f"[{_fmt_ts(m.get('ts'))}] {author}: {text}"
        reactions = m.get("reactions") or []
        if reactions:
            parts = []
            for r in reactions:
                who = [await self._person(u) for u in (r.get("users") or [])]
                parts.append(f":{r.get('name')}: by {', '.join(who) or '?'}")
            line += "\n    reactions: " + "; ".join(parts)
        return line

    async def _thread_text(self, channel: str, thread_ts: str) -> tuple[str, int]:
        """The joined text of one full thread, honestly truncated, plus the
        message count."""
        replies = await self._get(
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": _REPLIES_PER_THREAD},
        )
        messages = replies.get("messages") or []
        parts = []
        for m in messages:
            if not (m.get("text") or m.get("reactions")):
                continue
            parts.append(await self._render(m))
        if self._people is None:
            parts.append(
                "(note: user ids are shown as <@U...> because this app lacks the "
                "users:read scope; reactions and authors are still exact)"
            )
        joined = "\n---\n".join(parts)
        if len(joined) > _THREAD_CHARS:
            joined = joined[:_THREAD_CHARS] + "\n[thread truncated -- it continues past this point]"
        return joined, len(messages)

    async def _permalink(self, channel: str, thread_ts: str) -> str | None:
        try:
            data = await self._get(
                "chat.getPermalink",
                {"channel": channel, "message_ts": thread_ts},
            )
            return data.get("permalink")
        except Exception:
            return None

    async def _find_threads(
        self, email: str, channel: str, days, limit, around: str = "", window_days=None
    ) -> str:
        if not email or "@" not in email:
            return "I need a full email address to search for."
        channel_id, refusal = self._resolve_channel(channel)
        if refusal is not None:
            return refusal
        count = _capped_int(limit, _FIND_DEFAULT, _FIND_CAP)

        # The window is enforced by the Slack API itself (history's
        # oldest/latest bounds), never by walking pages until they age out
        # -- an anchored lookup costs one or two pages no matter how busy
        # the channel has been since. Anchored mode (`around`) is the
        # efficient path when a caller already holds an event timestamp:
        # audit-log first, then a tight window here.
        if around:
            try:
                anchor = datetime.fromisoformat(around.replace("Z", "+00:00"))
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=UTC)
            except ValueError:
                return (
                    f"I could not read {around!r} as a date -- use ISO form, "
                    'e.g. "2026-07-10".'
                )
            half = _capped_int(window_days, _WINDOW_DEFAULT, _WINDOW_CAP) * 86400
            oldest_bound = anchor.timestamp() - half
            latest_bound = anchor.timestamp() + half
            reach = f"{around} +/- {half // 86400} days"
        else:
            span = _capped_int(days, _DAYS_DEFAULT, _DAYS_CAP)
            oldest_bound = self._now() - span * 86400
            latest_bound = None
            reach = f"the last {span} days"

        matches: list[dict] = []
        seen: set[str] = set()
        scanned = 0
        cursor: str | None = None
        uid = await self._user_id_for(email)
        candidates: list[dict] = []  # roots with replies that did not name the person
        try:
            while scanned < self.scan_limit:
                params: dict = {
                    "channel": channel_id,
                    "limit": min(_PAGE_SIZE, self.scan_limit - scanned),
                    "oldest": f"{oldest_bound:.6f}",
                }
                if latest_bound is not None:
                    params["latest"] = f"{latest_bound:.6f}"
                if cursor:
                    params["cursor"] = cursor
                page = await self._get("conversations.history", params)
                messages = page.get("messages") or []
                scanned += len(messages)
                for m in messages:
                    thread_ts = m.get("thread_ts") or m.get("ts")
                    if not thread_ts or thread_ts in seen:
                        continue
                    root_text = m.get("text") or ""
                    if not (_matches(root_text, email) or (uid and f"<@{uid}" in root_text)):
                        if m.get("reply_count") and m.get("ts") == thread_ts:
                            candidates.append(
                                {"thread_ts": thread_ts, "root_ts": float(m.get("ts") or 0)}
                            )
                        continue
                    seen.add(thread_ts)
                    try:
                        root_ts = float(m.get("ts") or 0)
                    except (TypeError, ValueError):
                        root_ts = 0.0
                    matches.append({"thread_ts": thread_ts, "root_ts": root_ts})
                    if len(matches) >= count:
                        break
                if len(matches) >= count:
                    break
                cursor = (page.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as exc:
            return f"Could not read the channel history: {exc}"

        reply_note = ""
        if len(matches) < count and candidates:
            pool = [c for c in candidates if c["thread_ts"] not in seen][:_REPLY_SCAN_CAP]
            gate = asyncio.Semaphore(_REPLY_SCAN_CONCURRENCY)

            async def _hit(c: dict) -> dict | None:
                async with gate:
                    try:
                        data = await self._get(
                            "conversations.replies",
                            {
                                "channel": channel_id,
                                "ts": c["thread_ts"],
                                "limit": _REPLIES_PER_THREAD,
                            },
                        )
                    except Exception:
                        return None
                for r in (data.get("messages") or [])[1:]:
                    text = r.get("text") or ""
                    if _matches(text, email) or (uid and f"<@{uid}" in text):
                        return {**c, "in_reply": True}
                return None

            hits = [h for h in await asyncio.gather(*(_hit(c) for c in pool)) if h]
            for h in sorted(hits, key=lambda h: -h["root_ts"]):
                if len(matches) >= count:
                    break
                seen.add(h["thread_ts"])
                matches.append(h)
            clipped = len(candidates) > _REPLY_SCAN_CAP
            reply_note = (
                f" (also searched the replies of {len(pool)} threads"
                + (
                    f"; {len(candidates) - _REPLY_SCAN_CAP} older threads' replies "
                    "were not searched"
                    if clipped
                    else ""
                )
                + ")"
            )

        if not matches:
            budget_note = (
                f" (scan budget hit after {scanned} messages)"
                if scanned >= self.scan_limit
                else ""
            )
            return (
                f"No threads mentioning {email} in {channel_id} within "
                f"{reach}{budget_note}{reply_note}."
            )

        sections = []
        for m in matches:
            thread_ts = m["thread_ts"]
            try:
                text, n = await self._thread_text(channel_id, thread_ts)
            except Exception as exc:
                sections.append(
                    f"Thread {thread_ts} (started {_fmt_ts(m['root_ts'])}): "
                    f"could not read its replies: {exc}"
                )
                continue
            permalink = await self._permalink(channel_id, thread_ts)
            header = (
                f"Thread {thread_ts} (started {_fmt_ts(m['root_ts'])}, "
                f"{n} messages"
                + (
                    "; the person is named in a reply, not the opening message"
                    if m.get("in_reply")
                    else ""
                )
                + ")"
            )
            if permalink:
                header += f"\n{permalink}"
            sections.append(f"{header}\n{text}")
        found = len(matches)
        note = (
            f"\n\n(only the first {count} threads shown -- there may be more)"
            if found >= count
            else ""
        )
        return "\n\n=====\n\n".join(sections) + note

    async def _read_thread(self, channel: str, thread_ts: str) -> str:
        if not thread_ts:
            return "I need a thread timestamp to read."
        channel_id, refusal = self._resolve_channel(channel)
        if refusal is not None:
            return refusal
        try:
            text, n = await self._thread_text(channel_id, thread_ts)
        except Exception as exc:
            return f"Could not read that thread: {exc}"
        if not text:
            return f"Thread {thread_ts} in {channel_id} has no readable text."
        permalink = await self._permalink(channel_id, thread_ts)
        header = f"Thread {thread_ts} in {channel_id} ({n} messages)"
        if permalink:
            header += f"\n{permalink}"
        return f"{header}\n{text}"

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
