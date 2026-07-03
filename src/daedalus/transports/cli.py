"""The cli transport — a chat REPL, plus a one-shot helper for CI."""

from __future__ import annotations

import asyncio
import json

from daedalus.protocols import Handler
from daedalus.task import Message, Task, TaskState


class CliApprover:
    """Tier-2 gate approver for interactive sessions: show the plan, ask y/n.

    Headless runs keep the DenyAll default — approval requires a human
    who is actually present.
    """

    async def approve(self, action: str, plan: dict) -> bool:
        print(f"\n[approval required] {action}")
        print(json.dumps(plan, indent=2, default=str))
        answer = await asyncio.to_thread(input, "approve? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}


async def run_once(handler: Handler, text: str, *, context_id: str | None = None) -> Task:
    return await handler.handle_task(Task.from_text(text, context_id=context_id))


class CliTransport:
    def __init__(self, *, prompt: str = "you> ", reply_prefix: str = "agent> "):
        self.prompt = prompt
        self.reply_prefix = reply_prefix

    async def serve(self, handler: Handler) -> None:
        task = Task(context_id="cli")
        print("(interactive chat — 'exit' or Ctrl-D to quit)")
        while True:
            try:
                line = await asyncio.to_thread(input, self.prompt)
            except EOFError:
                print()
                return
            line = line.strip()
            if not line:
                continue
            if line.lower() in {"exit", "quit"}:
                return
            task.history.append(Message(role="user", text=line))
            task.state = TaskState.SUBMITTED
            try:
                task = await handler.handle_task(task)
            except Exception as exc:
                print(f"error: {exc}")
                continue
            print(f"{self.reply_prefix}{task.agent_text()}")
