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
# Transport-owned: the a2a transport stamps the verified JWT subject here
# after token verification and unconditionally discards any wire-supplied
# value, so handlers can trust it (daedalus.memory.teach gates on it).
VERIFIED_SUB_KEY = "_verified_sub"
# What personal memory recalls on, when the caller knows better than the prompt: a
# prompt that carries a large payload (a diff, a document) swamps the query with it.
# Absent, blank or not a string, recall uses the prompt text.
RECALL_KEY = "daedalus_recall"


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
    TOOL_BLOCKED = 703
    ESCALATION = 704


class ImagePart(BaseModel):
    """One image attached to a Message (agent-vision spec, wave 3).

    `data` is the raw base64 string -- no `data:` URI prefix; the LLM
    message builder (agent._messages) adds the provider wrapper. The A2A
    wire format deliberately does NOT carry these (task_to_a2a serializes
    text only; agent-to-agent image passing is a registered follow-up).
    """

    data: str
    media_type: str


class Message(BaseModel):
    role: Literal["user", "agent"]
    text: str
    # None (not []) so every existing Message(role=..., text=...) call site
    # -- from_text, reply, the cli/a2a/slack transports -- is untouched.
    images: list[ImagePart] | None = None
    # Who sent a user turn, as the transport knows them (Slack: the event's
    # user id, e.g. "U0123ABC"). None when the transport has no identity
    # to give. The learnings lane attributes candidates only to these ids,
    # never to a name the model read out of the text.
    sender: str | None = None


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

    def set_agent_text(self, text: str) -> None:
        """Overwrite the most recent agent message's text in place.

        Unlike reply(), this does not append a new turn or touch state /
        metadata -- it is the post_draft transform's mutator: the draft
        already in history becomes the lawful text, not an additional turn.
        """
        for message in reversed(self.history):
            if message.role == "agent":
                message.text = text
                return

    def discard_last_agent_message(self) -> None:
        """Remove the most recent agent message from history, in place.

        The post_draft transform's other mutator: when a hook blocks the
        draft, the draft must not merely lose the "current reply" spot --
        it must vanish from history entirely. task_to_a2a serializes the
        full history verbatim (a blocked draft would leak to a2a callers),
        and transports that persist the Task across turns (Slack threads,
        a2a taskId continuation) feed history back into the model's context
        on the next call. "Discarded, never partially sent."
        """
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index].role == "agent":
                del self.history[index]
                return

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
