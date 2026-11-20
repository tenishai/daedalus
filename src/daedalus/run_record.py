"""What a reply was built from, for post_draft hooks (#268).

A RunRecorder collects the trace events one task emits (Agent._emit feeds the
recorder in `current_recorder`), and `record()` turns them into a frozen
RunRecord. It is built from the trace, not from either engine, so it means the
same thing for the LiteLLM loop and the Claude Code engine.

A hook module may only import the standard library, so a hook reads the record
by attribute and never imports this module.

Usage is None when the provider did not report it. A total is None when any
model call in the run went unreported: a partial sum would read as a smaller
true number.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass

# Set by Agent.handle_task for the chat path; None everywhere else, so events
# from system tasks and background passes are not recorded.
current_recorder: contextvars.ContextVar[RunRecorder | None] = contextvars.ContextVar(
    "daedalus_run_recorder", default=None
)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call, or a run's total, as the engine reported
    them. None = not reported. Whether input_tokens includes the cache counts
    depends on the engine: see RunRecord.input_includes_cache, or read
    RunRecord.total_input_tokens, which accounts for it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool call. outcome is "ok", "empty", "error", "blocked", or "unknown"
    when the engine ran the tool itself and reported no result (Claude Code's
    built-ins). duration_s is None when there was no result to time."""

    name: str
    outcome: str
    duration_s: float | None


@dataclass(frozen=True)
class RunRecord:
    engine: str
    model: str | None
    turns: int  # model calls; for Claude Code, the turns it reported
    tool_calls: tuple[ToolCallRecord, ...]
    usage_per_turn: tuple[Usage, ...]  # empty when the engine reports only a total
    usage_total: Usage
    cost_usd: float | None  # only when the engine reports one (Claude Code)
    limit: str | None  # "steps", "repeats", "context", "budget", or None
    truncated: bool  # a reply was cut at max_tokens
    # LiteLLM's input_tokens already includes cache reads and writes; Claude Code's
    # (Anthropic's raw count) does not.
    input_includes_cache: bool

    @property
    def total_input_tokens(self) -> int | None:
        """All input the run sent, cached or not. None when unreported."""
        u = self.usage_total
        if u.input_tokens is None:
            return None
        if self.input_includes_cache:
            return u.input_tokens
        return u.input_tokens + (u.cache_read_input_tokens or 0) + (
            u.cache_creation_input_tokens or 0
        )


def tool_outcome(content: str) -> str:
    """Classify a tool result string by the prefixes Agent._call_tool and the
    tool providers produce (docs/arch/approvals.md)."""
    text = content.strip()
    if not text or text == "(no text content)":
        return "empty"
    if text.startswith("blocked:"):
        return "blocked"
    if text.startswith(("error:", "tool error:")):
        return "error"
    return "ok"


def _sum(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    if not values or len(known) != len(values):
        return None
    return sum(known)


class RunRecorder:
    """Accumulates one task's trace events. Not thread-safe: events arrive on
    the task's own event loop."""

    def __init__(self, engine: str, model: str | None):
        self.engine = engine
        self.model = model
        self._calls: list[list] = []  # [name, outcome, started, duration]
        self._usage: list[Usage] = []
        self._finished: dict | None = None
        self._limit: str | None = None
        self._truncated = False

    def observe(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "tool_call":
            self._calls.append([event.get("name", ""), "unknown", time.monotonic(), None])
        elif kind == "tool_result":
            name = event.get("name", "")
            for call in self._calls:  # oldest unanswered call with this name
                if call[0] == name and call[3] is None and call[1] == "unknown":
                    call[1] = tool_outcome(str(event.get("content", "")))
                    call[3] = time.monotonic() - call[2]
                    break
        elif kind == "usage":
            self._usage.append(Usage(**{f: event.get(f) for f in _USAGE_FIELDS}))
        elif kind == "engine_finished":
            self._finished = event
        elif kind == "loop_limit":
            self._limit = event.get("reason")
        elif kind == "truncated":
            self._truncated = True

    def record(self) -> RunRecord:
        finished = self._finished
        if finished is not None and not self._usage:
            # Claude Code reports one total for the whole run.
            total = Usage(**{f: finished.get(f) for f in _USAGE_FIELDS})
            turns = finished.get("turns") or 0
            cost = finished.get("total_cost_usd")
        else:
            total = Usage(**{f: _sum([getattr(u, f) for u in self._usage]) for f in _USAGE_FIELDS})
            turns = len(self._usage)
            cost = None
        return RunRecord(
            engine=self.engine,
            model=self.model,
            turns=turns,
            tool_calls=tuple(ToolCallRecord(c[0], c[1], c[3]) for c in self._calls),
            usage_per_turn=tuple(self._usage),
            usage_total=total,
            cost_usd=cost,
            limit=self._limit,
            truncated=self._truncated,
            input_includes_cache=finished is None or bool(self._usage),
        )
