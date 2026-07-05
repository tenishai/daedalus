"""Compose multiple tool providers behind the single ToolProvider seam."""

from __future__ import annotations

import warnings

from daedalus.protocols import ToolProvider


class MultiToolProvider:
    """Concatenates listings; routes calls to whichever provider listed the
    name. On a name collision the first provider wins (with a warning)."""

    def __init__(self, providers: list[ToolProvider]):
        self.providers = providers
        self._owner: dict[str, ToolProvider] = {}

    async def tools(self) -> list[dict]:
        specs: list[dict] = []
        self._owner = {}
        for provider in self.providers:
            for spec in await provider.tools():
                name = spec["name"]
                if name in self._owner:
                    warnings.warn(f"duplicate tool name {name!r}; first wins", stacklevel=2)
                    continue
                self._owner[name] = provider
                specs.append(spec)
        return specs

    async def call(self, name: str, payload: dict) -> str:
        if name not in self._owner:
            await self.tools()
        provider = self._owner.get(name)
        if provider is None:
            raise LookupError(f"unknown tool {name!r}")
        return await provider.call(name, payload)
