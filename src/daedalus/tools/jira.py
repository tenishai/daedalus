"""jira-backed tool provider -- a deployed agent reads issue-tracker tickets
through the room's broker, never with a credential of its own.

The same custody model as the github sidecar, applied to reads: the room
holds the tracker credential and exposes a handful of scoped read routes;
this provider calls them with the agent's colleague JWT and formats the
answer. The agent never sees the credential, and the room's broker pins
the reachable projects server-side -- nothing here can widen the scope,
because the scope was never a parameter.

Read-only by construction: the broker exposes no write route, and this
provider asks for none.

Mirrors colleagues.py's soft-degrade idiom throughout: a missing token env,
an unreachable broker, a refused route, and every tracker error all become
plain tool-result text; the provider never raises out to the agent loop.
"""

from __future__ import annotations

import os

_NOT_CONFIGURED = "The room's issue-tracker broker is not configured for me."
_NO_GRANT = (
    "The room has not granted me issue-tracker access. Ask my owner to "
    "enable the Jira read lane for me."
)

# Bounds. Each is a ceiling the tool states out loud when it clips, never a
# silent cut.
_SEARCH_DEFAULT = 10
_SEARCH_CAP = 25
_COMMENT_CHARS = 2_000
_BODY_CHARS = 4_000


class JiraToolProvider:
    def __init__(
        self,
        broker_url: str,
        token_env: str | None = None,
        self_name: str | None = None,
        client=None,
    ):
        self.broker_url = broker_url.rstrip("/")
        self.token_env = token_env
        self.self_name = self_name
        self._client = client  # injectable: tests pass a fake request client

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _agent_token(self) -> str | None:
        if not self.token_env:
            return None
        token = os.environ.get(self.token_env, "")
        return token or None

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "jira.get_issue",
                "description": (
                    "Read one issue-tracker ticket the room's broker allows: "
                    "summary, status, people, dates, labels, and its full "
                    "comment history (each comment with author and date). "
                    "Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": 'The issue key, e.g. "AREQ-123"',
                        },
                    },
                    "required": ["key"],
                },
            },
            {
                "name": "jira.find_issues",
                "description": (
                    "Search the issue-tracker project the room's broker "
                    "allows for tickets whose text mentions a term (a "
                    "person's email or name works well), newest first. "
                    "Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The term to search ticket text for",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"At most this many (default {_SEARCH_DEFAULT}, "
                                f"cap {_SEARCH_CAP})"
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "jira.get_issue":
            return await self._get_issue(payload.get("key", "") or "")
        if name == "jira.find_issues":
            return await self._find_issues(
                payload.get("text", "") or "", payload.get("limit")
            )
        raise LookupError(f"unknown tool {name!r}")

    async def _broker_get(self, path: str, params: dict | None = None):
        """(json-body, None) on success, (None, plain-text error) otherwise.
        The status-code vocabulary matches the github sidecar's _mint: 404
        is the room's anti-enumeration answer for 'no grant', everything
        else non-200 is a refusal named by code."""
        agent_token = self._agent_token()
        if agent_token is None or not self.self_name:
            return None, _NOT_CONFIGURED
        client = self._ensure_client()
        try:
            response = await client.get(
                f"{self.broker_url}/broker/agents/{self.self_name}{path}",
                headers={"Authorization": f"Bearer {agent_token}"},
                params=params or {},
            )
        except Exception as exc:
            return None, f"Could not reach the room's broker: {exc}"
        if response.status_code == 404:
            return None, _NO_GRANT
        if response.status_code != 200:
            return None, f"The broker refused: HTTP {response.status_code}."
        try:
            return response.json(), None
        except Exception:
            return None, "The broker answered in a shape I could not read."

    async def _get_issue(self, key: str) -> str:
        if not key:
            return "I need an issue key to read."
        body, error = await self._broker_get(f"/jira/issues/{key}")
        if error is not None:
            return error

        issue = body.get("issue") or {}
        lines = [
            f"{issue.get('key', key)}: {issue.get('summary', '(no summary)')}",
            f"status: {issue.get('status', '?')} ({issue.get('type', '?')})",
            f"reporter: {issue.get('reporter') or '?'}; "
            f"assignee: {issue.get('assignee') or 'unassigned'}",
            f"created: {issue.get('created_at') or '?'}; "
            f"updated: {issue.get('updated_at') or '?'}",
        ]
        if issue.get("resolution"):
            lines.append(
                f"resolution: {issue['resolution']} at {issue.get('resolved_at') or '?'}"
            )
        if issue.get("labels"):
            lines.append("labels: " + ", ".join(issue["labels"]))
        if issue.get("url"):
            lines.append(issue["url"])
        description = issue.get("description") or ""
        if description:
            if len(description) > _BODY_CHARS:
                description = description[:_BODY_CHARS] + "\n[description truncated]"
            lines.extend(["", description])

        comments = body.get("comments") or []
        if comments:
            lines.extend(["", f"comments ({len(comments)}):"])
            for c in comments:
                author = c.get("author") or "?"
                if c.get("author_email"):
                    author += f" <{c['author_email']}>"
                text = (c.get("body") or "").strip()
                if len(text) > _COMMENT_CHARS:
                    text = text[:_COMMENT_CHARS] + "\n  [comment truncated]"
                lines.append(f"- {author} at {c.get('created') or '?'}:")
                lines.append(f"  {text}" if text else "  (no text)")
            if body.get("comments_truncated"):
                lines.append("(older comments exist beyond what the broker returned)")
        else:
            lines.extend(["", "comments: none"])
        return "\n".join(lines)

    async def _find_issues(self, text: str, limit) -> str:
        if not text or len(text.strip()) < 3:
            return "I need at least a few characters of text to search for."
        count = min(int(limit or _SEARCH_DEFAULT), _SEARCH_CAP)
        body, error = await self._broker_get(
            "/jira/search", {"text": text.strip(), "limit": count}
        )
        if error is not None:
            return error
        issues = body.get("issues") or []
        if not issues:
            return f"No tickets whose text mentions {text.strip()!r}."
        lines = []
        for issue in issues:
            lines.append(
                f"{issue.get('key', '?')} [{issue.get('status', '?')}] "
                f"{issue.get('summary', '')} "
                f"(reporter {issue.get('reporter') or '?'}, "
                f"created {(issue.get('created_at') or '?')[:10]})"
            )
        if body.get("truncated"):
            lines.append(f"(only the first {count} shown -- there are more)")
        return "\n".join(lines)

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
