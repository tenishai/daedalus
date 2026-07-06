"""Task envelope — A2A-shaped states plus daedalus reason codes.

Phase 1 carries text-only messages; A2A Parts arrive with the a2a transport.
"""

from __future__ import annotations

import uuid
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field

REASON_KEY = "daedalus_reason_code"
# cabal reads metadata["cabal_reason_code"] (core/a2a/codes.py) — same 700-code
# taxonomy, shared lineage (CHP → cabal → daedalus). We write both keys so a
# cabal dispatching to a daedalus agent sees the reason without translation.
CABAL_REASON_KEY = "cabal_reason_code"


class TaskState(StrEnum):
    """A2A task states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ReasonCode(IntEnum):
    """Why a task stopped and what the caller should do about it."""

    HUMAN_INPUT_REQUIRED = 700
    POLICY_VIOLATION = 701
    BUDGET_EXHAUSTED = 702
    ESCALATION = 704


class Message(BaseModel):
    role: Literal["user", "agent"]
    text: str


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    context_id: str | None = None
    state: TaskState = TaskState.SUBMITTED
    history: list[Message] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, *, context_id: str | None = None) -> Task:
        return cls(context_id=context_id, history=[Message(role="user", text=text)])

    def user_text(self) -> str:
        """Text of the most recent user message, or empty string."""
        for message in reversed(self.history):
            if message.role == "user":
                return message.text
        return ""

    def agent_text(self) -> str:
        """Text of the most recent agent message, or empty string."""
        for message in reversed(self.history):
            if message.role == "agent":
                return message.text
        return ""

    def reply(
        self,
        text: str,
        *,
        state: TaskState = TaskState.COMPLETED,
        reason: ReasonCode | None = None,
    ) -> Task:
        self.history.append(Message(role="agent", text=text))
        self.state = state
        if reason is not None:
            self.metadata[REASON_KEY] = int(reason)
            self.metadata[CABAL_REASON_KEY] = int(reason)
        return self

    @property
    def reason_code(self) -> ReasonCode | None:
        raw = self.metadata.get(REASON_KEY, self.metadata.get(CABAL_REASON_KEY))
        return ReasonCode(raw) if raw is not None else None
