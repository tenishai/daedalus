"""github-backed tool provider — a deployed agent opens pull requests on the
repositories its room has approved for it, through the room's token broker.

Two-wall model (the room's PR-broker spec): the broker mints a ~1h GitHub
installation token scoped to exactly this agent's APPROVED repositories;
this provider fetches one per call, uses it, and forgets it. The agent
never holds a standing credential, and the token it briefly holds cannot
reach a repository the room has not granted.

PR-only by ruling: this provider can create a branch, put files on it, and
open a pull request. Nothing here merges, reviews, or touches a default
branch directly.

Attribution is stamped, not optional: the head branch is always
kobo/<agent>/<slug> and the PR body always ends with a footer naming the
agent and the room. The footer is a convention the provider enforces on
itself, not a security boundary -- the broker's audit trail is the record.

Mirrors colleagues.py's soft-degrade idiom throughout: a missing token env,
an unreachable broker, a refused mint, and every GitHub error all become
plain tool-result text; the provider never raises out to the agent loop.
"""

from __future__ import annotations

import base64
import os
import re
import secrets

_NOT_CONFIGURED = "The room's PR broker is not configured for me."
_NO_GRANTS = (
    "The room has not granted me any repositories yet. Ask my owner to "
    "request repo access on my Access tab."
)

_GITHUB_API = "https://api.github.com"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(title: str) -> str:
    """A branch-safe slug from the PR title: lowercase, dashes, bounded,
    plus a short random suffix so two same-titled PRs never collide."""
    base = _SLUG_RE.sub("-", title.lower()).strip("-")[:40].rstrip("-") or "change"
    return f"{base}-{secrets.token_hex(3)}"


class GitHubToolProvider:
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

            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _agent_token(self) -> str | None:
        if not self.token_env:
            return None
        token = os.environ.get(self.token_env, "")
        return token or None

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "github.open_pr",
                "description": (
                    "Open a pull request on a repository the room has "
                    "approved for you: creates a branch, writes the given "
                    "files onto it, and opens the PR against the default "
                    "branch. You cannot merge; a human reviews."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "title": {"type": "string", "description": "The PR title"},
                        "body": {
                            "type": "string",
                            "description": "The PR description (markdown)",
                        },
                        "files": {
                            "type": "array",
                            "description": (
                                "The complete new content of every file this "
                                "PR touches"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                            },
                        },
                    },
                    "required": ["repository", "title", "body", "files"],
                },
            }
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "github.open_pr":
            return await self._open_pr(
                payload.get("repository", ""),
                payload.get("title", ""),
                payload.get("body", ""),
                payload.get("files", []) or [],
            )
        raise LookupError(f"unknown tool {name!r}")

    async def _mint(self) -> tuple[dict | None, str | None]:
        """Ask the room's broker for a scoped token. (minted, None) on
        success, (None, plain-text error) otherwise."""
        agent_token = self._agent_token()
        if agent_token is None or not self.self_name:
            return None, _NOT_CONFIGURED
        client = self._ensure_client()
        try:
            response = await client.post(
                f"{self.broker_url}/broker/agents/{self.self_name}/github-token",
                headers={"Authorization": f"Bearer {agent_token}"},
            )
        except Exception as exc:
            return None, f"Could not reach the room's token broker: {exc}"
        if response.status_code == 404:
            return None, _NO_GRANTS
        if response.status_code != 200:
            return None, f"The token broker refused: HTTP {response.status_code}."
        try:
            minted = response.json()
            assert minted["token"] and isinstance(minted.get("repositories"), list)
        except Exception:
            return None, "The token broker answered in a shape I could not read."
        return minted, None

    async def _github(self, token: str, method: str, path: str, **kwargs):
        client = self._ensure_client()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        return await client.request(method, f"{_GITHUB_API}{path}", headers=headers, **kwargs)

    async def _open_pr(self, repository: str, title: str, body: str, files: list) -> str:
        if not repository or not title or not files:
            return "I need a repository, a title, and at least one file to open a PR."
        minted, error = await self._mint()
        if error is not None:
            return error
        if repository not in minted["repositories"]:
            granted = ", ".join(minted["repositories"]) or "none"
            return (
                f"I am not approved for {repository}. Repositories the room "
                f"has granted me: {granted}."
            )
        token = minted["token"]

        try:
            repo_response = await self._github(token, "GET", f"/repos/{repository}")
            if repo_response.status_code != 200:
                return (
                    f"GitHub would not show me {repository} "
                    f"(HTTP {repo_response.status_code}) -- it may not be "
                    "added to the room's app installation yet."
                )
            default_branch = repo_response.json()["default_branch"]

            ref_response = await self._github(
                token, "GET", f"/repos/{repository}/git/ref/heads/{default_branch}"
            )
            if ref_response.status_code != 200:
                return f"Could not read {repository}'s {default_branch} branch."
            base_sha = ref_response.json()["object"]["sha"]

            branch = f"kobo/{self.self_name}/{_slug(title)}"
            create_ref = await self._github(
                token,
                "POST",
                f"/repos/{repository}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if create_ref.status_code != 201:
                return f"Could not create a branch on {repository} (HTTP {create_ref.status_code})."

            for entry in files:
                path = entry.get("path", "")
                content = entry.get("content", "")
                if not path:
                    return "Every file needs a path."
                # An existing file's update needs its current blob sha; a
                # brand-new file must NOT send one. Read from the new branch
                # so the base branch is never written.
                existing = await self._github(
                    token,
                    "GET",
                    f"/repos/{repository}/contents/{path}",
                    params={"ref": branch},
                )
                put_body = {
                    "message": f"{title}\n\n[{self.self_name}, via the agent room]",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": branch,
                }
                if existing.status_code == 200:
                    put_body["sha"] = existing.json().get("sha", "")
                put_response = await self._github(
                    token, "PUT", f"/repos/{repository}/contents/{path}", json=put_body
                )
                if put_response.status_code not in (200, 201):
                    return (
                        f"Could not write {path} on {repository} "
                        f"(HTTP {put_response.status_code})."
                    )

            footer = (
                f"\n\n---\nOpened by agent **{self.self_name}** via the agent "
                "room. A human must review and merge; this agent cannot."
            )
            pr_response = await self._github(
                token,
                "POST",
                f"/repos/{repository}/pulls",
                json={
                    "title": title,
                    "head": branch,
                    "base": default_branch,
                    "body": body + footer,
                },
            )
            if pr_response.status_code != 201:
                return f"Could not open the PR on {repository} (HTTP {pr_response.status_code})."
            url = pr_response.json().get("html_url", "(no url)")
            return f"Opened: {url} (branch {branch}, against {default_branch})"
        except Exception as exc:
            return f"Opening the PR failed partway: {exc}"

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
