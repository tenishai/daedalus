"""The cli transport — a chat REPL, plus a one-shot helper for CI."""

from __future__ import annotations

import asyncio

from daedalus.protocols import Handler
from daedalus.task import Message, Task, TaskState


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
