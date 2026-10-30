"""Fakes for every Protocol — no network in unit tests."""

from __future__ import annotations

from daedalus.protocols import LLMResponse


class ScriptedLLM:
    """Returns canned replies in order and records every call it saw.

    A reply may be a str (plain text) or a prebuilt LLMResponse (tool calls).
    """

    def __init__(self, *replies: str | LLMResponse):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict] | None] = []

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse:
        self.calls.append(messages)
        self.tools_seen.append(tools)
        reply = self.replies.pop(0) if self.replies else "(script exhausted)"
        if isinstance(reply, LLMResponse):
            return reply
        return LLMResponse(text=reply, input_tokens=10, output_tokens=5)


class FakeTools:
    """A ToolProvider with fixed specs; records calls, returns canned results."""

    def __init__(self, specs: list[dict], results: dict[str, str] | None = None):
        self.specs = specs
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def tools(self) -> list[dict]:
        return self.specs

    async def call(self, name: str, payload: dict) -> str:
        self.calls.append((name, payload))
        return self.results.get(name, f"ok: {name}")


class RecordingApprover:
    """Approves or denies everything; records each plan it was shown."""

    def __init__(self, verdict: bool):
        self.verdict = verdict
        self.asked: list[tuple[str, dict]] = []

    async def approve(self, action: str, plan: dict) -> bool:
        self.asked.append((action, plan))
        return self.verdict


class CannedMemory:
    """recall() returns fixed items; commit() records what was written."""

    def __init__(self, *items: str):
        self.items = list(items)
        self.committed: list[tuple[str, str]] = []

    async def recall(self, query: str, k: int = 5) -> list[str]:
        return self.items[:k]

    async def commit(self, content: str, kind: str = "fact", tags: str = "") -> None:
        self.committed.append((content, kind, tags) if tags else (content, kind))
