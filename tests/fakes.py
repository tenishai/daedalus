"""Fakes for every Protocol — no network in unit tests."""

from __future__ import annotations

from daedalus.protocols import LLMResponse


class ScriptedLLM:
    """Returns canned replies in order and records every message list it saw."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def complete(self, messages: list[dict]) -> LLMResponse:
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else "(script exhausted)"
        return LLMResponse(text=text, input_tokens=10, output_tokens=5)


class CannedMemory:
    """recall() returns fixed items; commit() records what was written."""

    def __init__(self, *items: str):
        self.items = list(items)
        self.committed: list[tuple[str, str]] = []

    async def recall(self, query: str, k: int = 5) -> list[str]:
        return self.items[:k]

    async def commit(self, content: str, kind: str = "fact") -> None:
        self.committed.append((content, kind))
