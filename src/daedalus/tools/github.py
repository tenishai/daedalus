"""github-backed tool provider — a deployed agent opens pull requests on the
repositories its room has approved for it, through the room's token broker.

Two-wall model (the room's PR-broker spec): the broker mints a ~1h GitHub
installation token scoped to exactly this agent's APPROVED repositories;
this provider fetches one per call, uses it, and forgets it. The agent
never holds a standing credential, and the token it briefly holds cannot
reach a repository the room has not granted.

Writes are PR-only by ruling: this provider can create a branch, put files
on it, and open a pull request. Nothing here merges, reviews, or touches a
default branch directly. Alongside that one write path sit read tools
(list_prs, get_pr, list_releases, read_file) -- pure GETs under the same
mint-per-call scoped token, added 2026-08-28 so an agent can survey a repo
it has been granted (the client-schema release-survey use case) instead of
asking a human to paste the state in. Every read is bounded and marks its
own truncation honestly -- a silently clipped answer reads as complete,
which is worse than no answer.

Attribution is stamped, not optional: by default the head branch is named
kobo/<agent>/<slug> and the PR body always ends with a footer naming the
agent and the room. The footer is a convention the provider enforces on
itself, not a security boundary -- the broker's audit trail is the record.

Exact branch names (2026-09-04, the release-work ruling): real pipelines
key CI on branch patterns (release/*, feature/*, test-feature/*), which the
kobo/ prefix can never match -- so `open_pr` accepts an exact `branch`
name, and `push_branch` pushes a branch with no PR at all (a release cut
IS a bare branch push; the push itself is the trigger). The name prefix
was self-attribution, never a wall: the two walls stay exactly where they
were (the broker's repo pins and GitHub's own enforcement). What this
provider still refuses, in every path: touching a default branch, and
force-pushing over any existing branch.

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

# Read-tool bounds. Each one is a ceiling the tool states out loud when it
# clips, never a silent cut.
_LIST_PRS_CAP = 50
_LIST_PRS_DEFAULT = 20
_RELEASES_CAP = 20
_RELEASES_DEFAULT = 10
_PR_BODY_CHARS = 4000
_FILE_CHARS = 48_000
_DIR_ENTRIES_CAP = 50


def _slug(title: str) -> str:
    """A branch-safe slug from the PR title: lowercase, dashes, bounded,
    plus a short random suffix so two same-titled PRs never collide."""
    base = _SLUG_RE.sub("-", title.lower()).strip("-")[:40].rstrip("-") or "change"
    return f"{base}-{secrets.token_hex(3)}"


_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _branch_problem(name: str) -> str | None:
    """None when `name` is a branch name this provider will push, a plain
    refusal otherwise. Deliberately stricter than git's own ref rules --
    the goal is names a human would also write, not the full ref grammar."""
    if (
        not _BRANCH_RE.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith((".lock", "/", "."))
    ):
        return (
            f"{name!r} is not a branch name I can push (letters, digits, "
            "and . _ / - segments only; no leading dash, no '..')."
        )
    return None


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
                        "branch": {
                            "type": "string",
                            "description": (
                                "Exact head branch name (optional). Without "
                                "it the branch is named kobo/<you>/<slug>. "
                                "Use when CI keys on branch name patterns "
                                "(e.g. feature/*)."
                            ),
                        },
                    },
                    "required": ["repository", "title", "body", "files"],
                },
            },
            {
                "name": "github.push_branch",
                "description": (
                    "Push a new branch with an EXACT name to a repository "
                    "the room has approved for you -- no pull request. Use "
                    "only when a workflow keys on the branch name itself "
                    "(release cuts, CI branch patterns); otherwise prefer "
                    "github.open_pr. The branch starts at the default "
                    "branch's head (or `base`), optionally carrying one "
                    "commit with the given files. Refuses the default "
                    "branch and any branch that already exists -- never a "
                    "force-push."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "branch": {
                            "type": "string",
                            "description": (
                                'The exact branch name to create, e.g. '
                                '"release/2026/W36/01-minor"'
                            ),
                        },
                        "files": {
                            "type": "array",
                            "description": (
                                "Files to commit onto the new branch "
                                "(optional -- omit for a bare branch cut at "
                                "the base's head)"
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
                        "message": {
                            "type": "string",
                            "description": (
                                "Commit message when files are given "
                                "(optional)"
                            ),
                        },
                        "base": {
                            "type": "string",
                            "description": (
                                "Branch to start from (default: the "
                                "default branch)"
                            ),
                        },
                    },
                    "required": ["repository", "branch"],
                },
            },
            {
                "name": "github.list_prs",
                "description": (
                    "List pull requests on a repository the room has approved "
                    "for you. state may be open, closed, merged, or all "
                    "(merged means closed AND actually merged). Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed", "merged", "all"],
                            "description": "Which PRs to list (default open)",
                        },
                        "base": {
                            "type": "string",
                            "description": "Only PRs against this base branch",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"At most this many (default "
                                f"{_LIST_PRS_DEFAULT}, cap {_LIST_PRS_CAP})"
                            ),
                        },
                    },
                    "required": ["repository"],
                },
            },
            {
                "name": "github.get_pr",
                "description": (
                    "One pull request in detail: title, author, state, "
                    "branches, description, and the files it touches. "
                    "Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "number": {
                            "type": "integer",
                            "description": "The PR number",
                        },
                    },
                    "required": ["repository", "number"],
                },
            },
            {
                "name": "github.list_releases",
                "description": (
                    "List a repository's releases, newest first (tag, name, "
                    "published date). Falls back to bare git tags when the "
                    "repository publishes no GitHub releases. Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"At most this many (default "
                                f"{_RELEASES_DEFAULT}, cap {_RELEASES_CAP})"
                            ),
                        },
                    },
                    "required": ["repository"],
                },
            },
            {
                "name": "github.read_file",
                "description": (
                    "Read one file from a repository the room has approved "
                    "for you (optionally at a branch, tag, or commit). "
                    "Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": 'The repository, as "owner/name"',
                        },
                        "path": {
                            "type": "string",
                            "description": "The file path within the repository",
                        },
                        "ref": {
                            "type": "string",
                            "description": "Branch, tag, or commit (default: the default branch)",
                        },
                    },
                    "required": ["repository", "path"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "github.open_pr":
            return await self._open_pr(
                payload.get("repository", ""),
                payload.get("title", ""),
                payload.get("body", ""),
                payload.get("files", []) or [],
                payload.get("branch", "") or "",
            )
        if name == "github.push_branch":
            return await self._push_branch(
                payload.get("repository", ""),
                payload.get("branch", "") or "",
                payload.get("files", []) or [],
                payload.get("message", "") or "",
                payload.get("base", "") or "",
            )
        if name == "github.list_prs":
            return await self._list_prs(
                payload.get("repository", ""),
                payload.get("state", "open") or "open",
                payload.get("base", "") or "",
                payload.get("limit"),
            )
        if name == "github.get_pr":
            return await self._get_pr(
                payload.get("repository", ""), payload.get("number")
            )
        if name == "github.list_releases":
            return await self._list_releases(
                payload.get("repository", ""), payload.get("limit")
            )
        if name == "github.read_file":
            return await self._read_file(
                payload.get("repository", ""),
                payload.get("path", ""),
                payload.get("ref", "") or "",
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

    async def _scoped(self, repository: str) -> tuple[str | None, str | None]:
        """Mint a token and check `repository` is inside the grant -- the
        preamble every tool here shares. (token, None) when the agent may
        touch the repository, (None, plain-text refusal) otherwise."""
        minted, error = await self._mint()
        if error is not None:
            return None, error
        if repository not in minted["repositories"]:
            granted = ", ".join(minted["repositories"]) or "none"
            return None, (
                f"I am not approved for {repository}. Repositories the room "
                f"has granted me: {granted}."
            )
        return minted["token"], None

    async def _github(self, token: str, method: str, path: str, **kwargs):
        client = self._ensure_client()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        return await client.request(method, f"{_GITHUB_API}{path}", headers=headers, **kwargs)

    async def _open_pr(
        self, repository: str, title: str, body: str, files: list, branch_name: str = ""
    ) -> str:
        if not repository or not title or not files:
            return "I need a repository, a title, and at least one file to open a PR."
        if branch_name:
            problem = _branch_problem(branch_name)
            if problem is not None:
                return problem
        token, error = await self._scoped(repository)
        if error is not None:
            return error

        try:
            repo_response = await self._github(token, "GET", f"/repos/{repository}")
            if repo_response.status_code != 200:
                return (
                    f"GitHub would not show me {repository} "
                    f"(HTTP {repo_response.status_code}) -- it may not be "
                    "added to the room's app installation yet."
                )
            default_branch = repo_response.json()["default_branch"]
            if branch_name == default_branch:
                return (
                    f"I will not use {repository}'s default branch "
                    f"({default_branch}) as a PR head."
                )

            ref_response = await self._github(
                token, "GET", f"/repos/{repository}/git/ref/heads/{default_branch}"
            )
            if ref_response.status_code != 200:
                return f"Could not read {repository}'s {default_branch} branch."
            base_sha = ref_response.json()["object"]["sha"]

            branch = branch_name or f"kobo/{self.self_name}/{_slug(title)}"
            create_ref = await self._github(
                token,
                "POST",
                f"/repos/{repository}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if create_ref.status_code == 422 and branch_name:
                return (
                    f"Branch {branch!r} already exists on {repository}; I "
                    "will not push over an existing branch."
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

    async def _push_branch(
        self, repository: str, branch: str, files: list, message: str, base: str
    ) -> str:
        if not repository or not branch:
            return "I need a repository and a branch name."
        problem = _branch_problem(branch)
        if problem is not None:
            return problem
        token, error = await self._scoped(repository)
        if error is not None:
            return error

        try:
            repo_response = await self._github(token, "GET", f"/repos/{repository}")
            if repo_response.status_code != 200:
                return (
                    f"GitHub would not show me {repository} "
                    f"(HTTP {repo_response.status_code}) -- it may not be "
                    "added to the room's app installation yet."
                )
            default_branch = repo_response.json()["default_branch"]
            if branch == default_branch:
                return (
                    f"I will not push to {repository}'s default branch "
                    f"({default_branch})."
                )

            existing = await self._github(
                token, "GET", f"/repos/{repository}/git/ref/heads/{branch}"
            )
            if existing.status_code == 200:
                return (
                    f"Branch {branch!r} already exists on {repository}; I "
                    "will not push over an existing branch."
                )

            base_ref = base or default_branch
            ref_response = await self._github(
                token, "GET", f"/repos/{repository}/git/ref/heads/{base_ref}"
            )
            if ref_response.status_code != 200:
                return f"Could not read {repository}'s {base_ref} branch."
            base_sha = ref_response.json()["object"]["sha"]

            create_ref = await self._github(
                token,
                "POST",
                f"/repos/{repository}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if create_ref.status_code != 201:
                return (
                    f"Could not create {branch!r} on {repository} "
                    f"(HTTP {create_ref.status_code})."
                )

            for entry in files:
                path = entry.get("path", "")
                content = entry.get("content", "")
                if not path:
                    return (
                        f"Branch {branch} was created at {base_sha[:7]}, but "
                        "one of the files has no path -- nothing was "
                        "committed onto it."
                    )
                # Same read-then-put dance as _open_pr: an existing file's
                # update needs its current blob sha, a new file must not
                # send one, and reads come from the new branch only.
                existing_file = await self._github(
                    token,
                    "GET",
                    f"/repos/{repository}/contents/{path}",
                    params={"ref": branch},
                )
                commit_message = message or f"Changes on {branch}"
                put_body = {
                    "message": f"{commit_message}\n\n[{self.self_name}, via the agent room]",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": branch,
                }
                if existing_file.status_code == 200:
                    put_body["sha"] = existing_file.json().get("sha", "")
                put_response = await self._github(
                    token, "PUT", f"/repos/{repository}/contents/{path}", json=put_body
                )
                if put_response.status_code not in (200, 201):
                    return (
                        f"Branch {branch} was created at {base_sha[:7]}, but "
                        f"writing {path} failed (HTTP {put_response.status_code})."
                    )

            committed = (
                f" with {len(files)} file{'s' if len(files) != 1 else ''} committed"
                if files
                else ""
            )
            return (
                f"Pushed branch {branch} on {repository} at {base_ref} head "
                f"({base_sha[:7]}){committed}. No pull request was opened."
            )
        except Exception as exc:
            return f"Pushing the branch failed partway: {exc}"

    async def _list_prs(self, repository: str, state: str, base: str, limit) -> str:
        if not repository:
            return "I need a repository to list pull requests."
        if state not in ("open", "closed", "merged", "all"):
            return "state must be open, closed, merged, or all."
        count = min(int(limit or _LIST_PRS_DEFAULT), _LIST_PRS_CAP)
        token, error = await self._scoped(repository)
        if error is not None:
            return error
        # "merged" is GitHub's state=closed with merged_at set -- fetch a
        # full page so the post-filter still has enough to satisfy `count`.
        query_state = "closed" if state == "merged" else state
        params = {"state": query_state, "per_page": 100, "sort": "created", "direction": "desc"}
        if base:
            params["base"] = base
        try:
            response = await self._github(
                token, "GET", f"/repos/{repository}/pulls", params=params
            )
            if response.status_code != 200:
                return f"GitHub would not list PRs on {repository} (HTTP {response.status_code})."
            pulls = response.json()
            if state == "merged":
                pulls = [p for p in pulls if p.get("merged_at")]
            clipped = len(pulls) > count
            pulls = pulls[:count]
            if not pulls:
                return f"No {state} pull requests on {repository}."
            lines = []
            for p in pulls:
                if p.get("merged_at"):
                    status = f"merged {p['merged_at'][:10]}"
                else:
                    status = p.get("state", "?")
                author = (p.get("user") or {}).get("login", "?")
                lines.append(f"#{p.get('number')} [{status}] {p.get('title', '')} ({author})")
            if clipped:
                lines.append(f"(only the first {count} shown -- there are more)")
            return "\n".join(lines)
        except Exception as exc:
            return f"Listing PRs failed partway: {exc}"

    async def _get_pr(self, repository: str, number) -> str:
        if not repository or not isinstance(number, int):
            return "I need a repository and a PR number."
        token, error = await self._scoped(repository)
        if error is not None:
            return error
        try:
            response = await self._github(
                token, "GET", f"/repos/{repository}/pulls/{number}"
            )
            if response.status_code != 200:
                return (
                    f"GitHub would not show me PR #{number} on {repository} "
                    f"(HTTP {response.status_code})."
                )
            p = response.json()
            status = f"merged {p['merged_at'][:10]}" if p.get("merged_at") else p.get("state", "?")
            body = p.get("body") or "(no description)"
            if len(body) > _PR_BODY_CHARS:
                body = body[:_PR_BODY_CHARS] + "\n[description truncated]"
            head = (p.get("head") or {}).get("ref", "?")
            base_ref = (p.get("base") or {}).get("ref", "?")
            author = (p.get("user") or {}).get("login", "?")
            lines = [
                f"#{number} [{status}] {p.get('title', '')}",
                f"author: {author}; {head} -> {base_ref}",
                "",
                body,
            ]
            files_response = await self._github(
                token,
                "GET",
                f"/repos/{repository}/pulls/{number}/files",
                params={"per_page": 100},
            )
            if files_response.status_code == 200:
                files = files_response.json()
                lines.append("")
                lines.append("files:")
                lines.extend(
                    f"  {f.get('filename')} ({f.get('status')}, "
                    f"+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                    for f in files
                )
                if len(files) == 100:
                    lines.append("  (only the first 100 files shown -- there are more)")
            return "\n".join(lines)
        except Exception as exc:
            return f"Reading the PR failed partway: {exc}"

    async def _list_releases(self, repository: str, limit) -> str:
        if not repository:
            return "I need a repository to list releases."
        count = min(int(limit or _RELEASES_DEFAULT), _RELEASES_CAP)
        token, error = await self._scoped(repository)
        if error is not None:
            return error
        try:
            response = await self._github(
                token,
                "GET",
                f"/repos/{repository}/releases",
                params={"per_page": count},
            )
            if response.status_code != 200:
                return (
                    f"GitHub would not list releases on {repository} "
                    f"(HTTP {response.status_code})."
                )
            releases = response.json()
            if releases:
                lines = []
                for r in releases[:count]:
                    tag = r.get("tag_name", "?")
                    name = r.get("name") or ""
                    published = (r.get("published_at") or "")[:10]
                    suffix = " (prerelease)" if r.get("prerelease") else ""
                    label = f" {name}" if name and name != tag else ""
                    lines.append(f"{tag}{label} ({published}){suffix}")
                return "\n".join(lines)
            # A repo that only pushes bare tags publishes no releases at all
            # -- surveying "since the last release" still needs those tags.
            tags_response = await self._github(
                token, "GET", f"/repos/{repository}/tags", params={"per_page": count}
            )
            if tags_response.status_code != 200 or not tags_response.json():
                return f"No releases or tags on {repository}."
            lines = [f"{repository} publishes no GitHub releases; most recent tags:"]
            lines.extend(
                f"{t.get('name', '?')} ({(t.get('commit') or {}).get('sha', '')[:7]})"
                for t in tags_response.json()[:count]
            )
            return "\n".join(lines)
        except Exception as exc:
            return f"Listing releases failed partway: {exc}"

    async def _read_file(self, repository: str, path: str, ref: str) -> str:
        if not repository or not path:
            return "I need a repository and a file path."
        token, error = await self._scoped(repository)
        if error is not None:
            return error
        try:
            params = {"ref": ref} if ref else {}
            response = await self._github(
                token, "GET", f"/repos/{repository}/contents/{path}", params=params
            )
            if response.status_code != 200:
                return (
                    f"GitHub would not show me {path} on {repository} "
                    f"(HTTP {response.status_code})."
                )
            data = response.json()
            if isinstance(data, list):
                names = [e.get("name", "?") for e in data[:_DIR_ENTRIES_CAP]]
                listing = "\n".join(f"  {n}" for n in names)
                more = (
                    f"\n  (only the first {_DIR_ENTRIES_CAP} entries shown)"
                    if len(data) > _DIR_ENTRIES_CAP
                    else ""
                )
                return f"{path} is a directory:\n{listing}{more}"
            if data.get("type") != "file" or data.get("encoding") != "base64":
                return f"{path} on {repository} is not a readable file."
            try:
                text = base64.b64decode(data.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return f"{path} on {repository} is not text I can read."
            if len(text) > _FILE_CHARS:
                return text[:_FILE_CHARS] + "\n[file truncated -- it continues past this point]"
            return text
        except Exception as exc:
            return f"Reading the file failed partway: {exc}"

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
