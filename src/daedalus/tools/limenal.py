"""limenal-backed tool provider — the tenish gateway behind the ToolProvider seam.

One MCP connection, many handlers; credential scoping lives server-side.
limenal (github.com/dimartin/limenal) is not on PyPI; install alongside:
pip install -e path/to/limenal. Tool names arrive already dotted
(handler.tool), matching daedalus's canonical form with no munging.
"""

from __future__ import annotations

import os


class LimenalToolProvider:
    def __init__(self, url: str, token_env: str | None = None, client=None):
        self.url = url
        self.token_env = token_env
        self._client = client  # injectable: tests pass fakes / ASGI-backed clients

    def _ensure(self):
        if self._client is None:
            from limenal.client import AsyncMCPClient

            token = os.environ.get(self.token_env, "") if self.token_env else ""
            # An empty bearer renders "Authorization: Bearer " (trailing
            # space) which httpx rejects as an illegal header (finding #16).
            # AUTH_DISABLED servers ignore the value, so send a placeholder.
            token = token or "no-auth"
            self._client = AsyncMCPClient(self.url, token_provider=lambda: token)
        return self._client

    async def tools(self) -> list[dict]:
        client = self._ensure()
        try:
            listed = await client.list_tools()
        except Exception as exc:
            # Name the gateway and its address — "connection failed" without
            # a WHERE sends the operator hunting (finding #23: the person who
            # always forgets to start limenal deserves an error that says so).
            raise RuntimeError(
                f"limenal at {self.url} is unreachable ({exc}) — is it running? "
                f"e.g. LIMENAL_AUTH_DISABLED=1 limenal serve --port <port>"
            ) from exc
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            }
            for tool in listed
        ]

    async def call(self, name: str, payload: dict) -> str:
        client = self._ensure()
        outcome = await client.call_tool(name, payload)
        text = outcome.text or "(no text content)"
        if outcome.is_error:
            raise RuntimeError(text)
        return text

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
