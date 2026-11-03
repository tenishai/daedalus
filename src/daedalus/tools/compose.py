"""Compose multiple tool providers behind the single ToolProvider seam."""

from __future__ import annotations

import warnings

from daedalus.protocols import ToolProvider, ToolProviderRefused


class MultiToolProvider:
    """Concatenates listings; routes calls to whichever provider listed the
    name. On a name collision the first provider wins (with a warning)."""

    def __init__(self, providers: list[ToolProvider]):
        self.providers = providers
        self._owner: dict[str, ToolProvider] = {}
        self._warned: set[str] = set()
        # Why each refusing provider's tools are missing from the last listing.
        self.refused: list[str] = []

    async def tools(self) -> list[dict]:
        specs: list[dict] = []
        self._owner = {}
        self.refused = []
        for provider in self.providers:
            try:
                listed = await provider.tools()
            except ToolProviderRefused as exc:
                # One gateway refusing this caller is not a reason to fail a
                # turn the other providers can serve (daedalus#156). An outage
                # still raises.
                self.refused.append(str(exc))
                continue
            for spec in listed:
                name = spec["name"]
                if name in self._owner:
                    if name not in self._warned:  # once, not every turn
                        self._warned.add(name)
                        warnings.warn(
                            f"duplicate tool name {name!r}; first wins", stacklevel=2
                        )
                    continue
                self._owner[name] = provider
                specs.append(spec)
        return specs

    def provider_of(self, name: str) -> ToolProvider | None:
        """The provider that listed `name` in the last tools() call, or None.
        Agent._knowledge_only reads its `knowledge_only` attribute (kobo#310)."""
        return self._owner.get(name)

    async def call(self, name: str, payload: dict) -> str:
        if name not in self._owner:
            await self.tools()
        provider = self._owner.get(name)
        if provider is None:
            raise LookupError(f"unknown tool {name!r}")
        return await provider.call(name, payload)
