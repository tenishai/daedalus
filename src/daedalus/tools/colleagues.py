"""colleagues-backed tool provider — a deployed agent finds and asks the
colleagues listed in a switchboard directory, over A2A.

Mirrors liminal.py's token_env soft-degrade idiom: an absent or empty
bearer env var never fails at boot, only politely at call time. HTTP
failures, malformed replies, and self-referential asks all become plain
tool-result text — the provider never raises out to the agent loop.
"""

from __future__ import annotations

import os
import secrets

_NOT_CONFIGURED = "The colleague pass is not configured."
_UNREADABLE_REPLY = "The colleague answered in a shape I could not read."


def _text_parts_of(message: dict) -> str:
    """Join ALL text parts, in order — the same contract as
    transports.a2a.text_of, mirrored here rather than imported because that
    module requires the a2a extra (fastapi) and this provider must not.
    Taking parts[0] alone would silently drop text the day a peer emits a
    multi-part reply."""
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    return "\n".join(t for t in texts if t)


def _format_colleague(agent: dict) -> str:
    name = agent.get("name", "unknown")
    team = agent.get("team") or "unspecified"
    tags = agent.get("purpose_tags") or []
    tags_text = ", ".join(str(tag) for tag in tags) if tags else "none"
    capability = agent.get("capability") or "unspecified"
    return (
        f"{name} (team: {team})\n"
        f"purpose tags: {tags_text}\n"
        f"capability: {capability}"
    )


class ColleaguesToolProvider:
    def __init__(
        self,
        url: str,
        token_env: str | None = None,
        self_name: str | None = None,
        client=None,
    ):
        self.url = url.rstrip("/")
        self.token_env = token_env
        self.self_name = self_name
        self._client = client  # injectable: tests pass a fake get/post client

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=180.0)
        return self._client

    def _token(self) -> str | None:
        if not self.token_env:
            return None
        token = os.environ.get(self.token_env, "")
        return token or None

    async def tools(self) -> list[dict]:
        return [
            {
                "name": "colleagues.list_colleagues",
                "description": (
                    "List your colleagues from the switchboard directory: "
                    "their name, team, purpose tags, and capability."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "colleagues.ask_colleague",
                "description": "Ask a named colleague a question over A2A and get their reply.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The colleague's name, as listed in the directory",
                        },
                        "question": {
                            "type": "string",
                            "description": "The question to ask them",
                        },
                    },
                    "required": ["name", "question"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "colleagues.list_colleagues":
            return await self._list_colleagues()
        if name == "colleagues.ask_colleague":
            return await self._ask_colleague(payload.get("name", ""), payload.get("question", ""))
        raise LookupError(f"unknown tool {name!r}")

    async def _list_colleagues(self) -> str:
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = await client.get(f"{self.url}/directory", headers=headers)
        except Exception as exc:
            return f"Could not reach the colleague directory: {exc}"
        if response.status_code != 200:
            return f"The colleague directory returned an error: HTTP {response.status_code}."
        try:
            data = response.json()
            agents = data.get("agents", []) if isinstance(data, dict) else []
        except Exception:
            return _UNREADABLE_REPLY
        lines = [
            _format_colleague(agent)
            for agent in agents
            if isinstance(agent, dict) and agent.get("name") != self.self_name
        ]
        if not lines:
            return "No colleagues are listed yet."
        return "\n\n".join(lines)

    async def _ask_colleague(self, target: str, question: str) -> str:
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        if target == self.self_name:
            return "I cannot ask myself a question. Try asking one of my colleagues instead."
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {token}"}
        # Dialect note: this is the SWITCHBOARD's inbound routing envelope
        # ({target, task}), POSTed to the switchboard's /a2a/tasks. Daedalus's
        # own peer surface serves the same path but takes the bare Task
        # (design.md §8) — same route string, two different callers.
        envelope = {
            "target": target,
            "task": {
                "kind": "task",
                "id": f"ask-{secrets.token_hex(8)}",
                "status": {
                    "state": "submitted",
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "parts": [{"kind": "text", "text": question}],
                    },
                },
                "history": [],
            },
        }
        try:
            response = await client.post(
                f"{self.url}/a2a/tasks", json=envelope, headers=headers, timeout=180.0
            )
        except Exception as exc:
            return f"Could not reach {target}: {exc}"
        if response.status_code != 200:
            return f"Asking {target} failed: HTTP {response.status_code}."
        try:
            data = response.json()
            text = _text_parts_of(data["status"]["message"])
        except Exception:
            return _UNREADABLE_REPLY
        if not text:
            return _UNREADABLE_REPLY
        return text

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
