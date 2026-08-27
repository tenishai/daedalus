"""colleagues-backed tool provider — a deployed agent finds and asks the
colleagues listed in a switchboard directory, over A2A.

Mirrors liminal.py's token_env soft-degrade idiom: an absent or empty
bearer env var never fails at boot, only politely at call time. HTTP
failures, malformed replies, and self-referential asks all become plain
tool-result text — the provider never raises out to the agent loop.
"""

from __future__ import annotations

import contextvars
import os
import secrets
from pathlib import Path

_NOT_CONFIGURED = "The colleague pass is not configured."
_UNREADABLE_REPLY = "The colleague answered in a shape I could not read."
_NO_MEMORY = "I have no memory store configured, so I cannot remember that."

# The task context id currently being handled, set by Agent.handle_task.
# The switchboard's DoomLoopGuardrail is default-on but groups hops by
# contextId; without a threaded id each hop looks like a fresh conversation
# and the guard never fires. A contextvar rather than provider state: tools
# are invoked with arguments only, and an agent may handle tasks
# concurrently.
current_context_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_context_id", default=None
)


def _text_parts_of(message: dict) -> str:
    """Join ALL text parts, in order — the same contract as
    transports.a2a.text_of, mirrored here rather than imported because that
    module requires the a2a extra (fastapi) and this provider must not.
    Taking parts[0] alone would silently drop text the day a peer emits a
    multi-part reply."""
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    return "\n".join(t for t in texts if t)


class ColleaguesNotConfigured(Exception):
    """Raised by fetch_agents() when no token is configured.

    fetch_agents() is public: daedalus.memory.revision's colleague-quorum
    branch calls it directly (over the ColleaguesLike Protocol) to reuse
    this provider's exact directory fetch. This provider's own tool-facing
    callers (_list_colleagues, _remember) already check the token first and
    return their own polite not-configured text before ever calling
    fetch_agents(), so they never see this exception; a caller that skips
    that pre-check (or calls fetch_agents() directly, as revision.py does)
    gets a typed error instead of a request sent with "Authorization: Bearer
    None".
    """


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
        db_path: str | Path | None = None,
    ):
        self.url = url.rstrip("/")
        self.token_env = token_env
        self.self_name = self_name
        self._client = client  # injectable: tests pass a fake get/post client
        # The agent's techne db, threaded in at wiring time (Agent._tools_from_spec,
        # from the memory store's own db_path). None = no techne memory configured;
        # remember() then degrades politely instead of failing at boot.
        self.db_path = Path(db_path) if db_path is not None else None

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
            {
                "name": "colleagues.remember",
                "description": (
                    "Remember a fact a colleague told you. Stored at low confidence, "
                    "with the colleague credited as its source."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The fact to remember, verbatim",
                        },
                        "learned_from": {
                            "type": "string",
                            "description": "The colleague's name, as listed in the directory",
                        },
                    },
                    "required": ["fact", "learned_from"],
                },
            },
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name == "colleagues.list_colleagues":
            return await self._list_colleagues()
        if name == "colleagues.ask_colleague":
            return await self._ask_colleague(payload.get("name", ""), payload.get("question", ""))
        if name == "colleagues.remember":
            return await self._remember(payload.get("fact", ""), payload.get("learned_from", ""))
        raise LookupError(f"unknown tool {name!r}")

    async def fetch_agents(self) -> tuple[list[dict] | None, str | None]:
        """GET the switchboard directory. (agents, None) on success, or
        (None, plain-text error) on any failure -- the one request shape
        both list_colleagues and remember validate names against.

        Public and token-gated: raises ColleaguesNotConfigured before ever
        touching the network if no token is configured, the same gate every
        other network-touching call on this provider applies. Public
        because daedalus.memory.revision's colleague-quorum branch reuses
        this exact fetch (see the ColleaguesLike Protocol there) to
        corroborate colleague-sourced candidates over the same client and
        config the model's own colleagues.* tools use.
        """
        token = self._token()
        if token is None:
            raise ColleaguesNotConfigured("the colleague pass is not configured")
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = await client.get(f"{self.url}/directory", headers=headers)
        except Exception as exc:
            return None, f"Could not reach the colleague directory: {exc}"
        if response.status_code != 200:
            return None, f"The colleague directory returned an error: HTTP {response.status_code}."
        try:
            data = response.json()
            agents = data.get("agents", []) if isinstance(data, dict) else []
        except Exception:
            return None, _UNREADABLE_REPLY
        return agents, None

    async def _list_colleagues(self) -> str:
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        agents, error = await self.fetch_agents()
        if error is not None:
            return error
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
        context_id = current_context_id.get()
        if context_id is not None:
            envelope["task"]["contextId"] = context_id
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

    async def _remember(self, fact: str, learned_from: str) -> str:
        """Remember a fact a named colleague told the agent: low confidence,
        provenance stamped to the colleague (and their team, when known).

        Refuses two shapes without ever touching techne: crediting itself
        (self_name), and crediting a name the switchboard directory doesn't
        list. Every other failure -- a down directory, a missing techne db,
        a store() that raises -- degrades to plain text, same as its sibling
        tools; this provider never raises out to the agent loop.
        """
        token = self._token()
        if token is None:
            return _NOT_CONFIGURED
        if learned_from == self.self_name:
            return "I cannot credit myself with teaching me something. Ask a colleague instead."
        if self.db_path is None:
            return _NO_MEMORY
        agents, error = await self.fetch_agents()
        if error is not None:
            return error
        match = next(
            (
                agent
                for agent in agents
                if isinstance(agent, dict) and agent.get("name") == learned_from
            ),
            None,
        )
        if match is None:
            return (
                f"I don't have a colleague named {learned_from} in the directory, "
                "so I cannot credit them."
            )
        team = match.get("team") or ""
        source = f"colleague:{learned_from}#team={team}" if team else f"colleague:{learned_from}"
        try:
            from techne.core import knowledge

            knowledge.store(self.db_path, fact, type="fact", confidence="low", source=source)
        except Exception as exc:
            return f"I could not remember that: {exc}"
        return f"Remembered, crediting {learned_from}."

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
