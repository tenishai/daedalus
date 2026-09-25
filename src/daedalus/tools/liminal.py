"""liminal-backed tool provider — the tenish gateway behind the ToolProvider seam.

One MCP connection, many handlers; credential scoping lives server-side.
liminal (github.com/tenishai/liminal) is not on PyPI; install alongside:
pip install -e path/to/liminal. Tool names arrive already dotted
(handler.tool), matching daedalus's canonical form with no munging.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

# Refresh the exchanged token this many seconds before it expires -- a
# token that dies mid-call helps nobody.
_EXCHANGE_SLACK_S = 60.0


class LiminalToolProvider:
    def __init__(
        self,
        url: str,
        token_env: str | None = None,
        client=None,
        exchange_url: str | None = None,
        exchange_client=None,
    ):
        self.url = url
        self.token_env = token_env
        self.exchange_url = exchange_url.rstrip("/") if exchange_url else None
        self._client = client  # injectable: tests pass fakes / ASGI-backed clients
        self._exchange_client = exchange_client  # injectable likewise
        self._bearer: str = ""
        self._bearer_expires: float = 0.0  # epoch; static bearers never expire

    async def _refresh_bearer(self) -> None:
        """Make self._bearer current.

        Static mode (no exchange_url): the env var IS the bearer, read
        once. Exchange mode (external-MCP arc, C1): the env var holds the
        agent's STANDING colleague token, which must never dial the
        gateway itself -- it is swapped at the switchboard for a
        short-lived gateway-audience token, cached and re-swapped shortly
        before expiry.
        """
        if self.exchange_url is None:
            if not self._bearer:
                token = os.environ.get(self.token_env, "") if self.token_env else ""
                # An empty bearer renders "Authorization: Bearer " (trailing
                # space) which httpx rejects as an illegal header (finding
                # #16). AUTH_DISABLED servers ignore the value, so send a
                # placeholder.
                self._bearer = token or "no-auth"
            return

        if self._bearer and time.time() < self._bearer_expires - _EXCHANGE_SLACK_S:
            return
        colleague = os.environ.get(self.token_env, "") if self.token_env else ""
        if not colleague:
            raise RuntimeError(
                "the gateway token exchange needs my colleague token and "
                f"${self.token_env or 'token_env'} is empty -- the bundle wiring "
                "is incomplete"
            )
        if self._exchange_client is None:
            import httpx

            self._exchange_client = httpx.AsyncClient(timeout=15.0)
        response = await self._exchange_client.post(
            f"{self.exchange_url}/auth/liminal-token",
            headers={"Authorization": f"Bearer {colleague}"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"the switchboard at {self.exchange_url} refused the gateway "
                f"token exchange (HTTP {response.status_code})"
            )
        body = response.json()
        self._bearer = body["access_token"]
        try:
            self._bearer_expires = datetime.fromisoformat(body["expires_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            # An unreadable expiry must fail SAFE for freshness: treat the
            # token as already stale so the next use re-exchanges, rather
            # than trusting a phantom long lifetime.
            self._bearer_expires = 0.0

    def _ensure(self):
        if self._client is None:
            from liminal.client import AsyncMCPClient

            self._client = AsyncMCPClient(self.url, token_provider=lambda: self._bearer)
        return self._client

    async def tools(self) -> list[dict]:
        await self._refresh_bearer()
        client = self._ensure()
        try:
            listed = await client.list_tools()
        except Exception as exc:
            # Name the gateway and its address — "connection failed" without
            # a WHERE sends the operator hunting (finding #23: the person who
            # always forgets to start liminal deserves an error that says so).
            raise RuntimeError(
                f"liminal at {self.url} is unreachable ({exc}) — is it running? "
                f"e.g. LIMINAL_AUTH_DISABLED=1 liminal serve --port <port>"
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
        await self._refresh_bearer()
        client = self._ensure()
        outcome = await client.call_tool(name, payload)
        text = outcome.text or "(no text content)"
        if outcome.is_error:
            raise RuntimeError(text)
        return text

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
        if self._exchange_client is not None and hasattr(self._exchange_client, "aclose"):
            await self._exchange_client.aclose()
