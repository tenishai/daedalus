"""The cli transport — a chat REPL, plus a one-shot helper for CI."""

from __future__ import annotations

import asyncio
import json
import select
import sys

from daedalus.protocols import Handler
from daedalus.task import RECALL_KEY, Message, Task, TaskState


def read_message(prompt: str, stream=None) -> str | None:
    """Read one user message; a multi-line paste becomes ONE message.

    After the first line, drain whatever the terminal already buffered
    (pasted lines arrive together; typed lines don't). Returns None on EOF.
    """
    stream = stream if stream is not None else sys.stdin
    print(prompt, end="", flush=True)
    first = stream.readline()
    if first == "":
        return None
    lines = [first.rstrip("\n")]
    try:
        fd = stream.fileno()
        while select.select([fd], [], [], 0.08)[0]:
            more = stream.readline()
            if more == "":
                break
            lines.append(more.rstrip("\n"))
    except (OSError, ValueError):
        pass  # non-selectable stream: single-line mode
    if len(lines) > 1:
        print(f"({len(lines)} lines captured)")
    return "\n".join(lines).strip()


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


async def run_once(
    handler: Handler,
    text: str,
    *,
    context_id: str | None = None,
    recall: str | None = None,
    metadata: dict | None = None,
) -> Task:
    task = Task.from_text(text, context_id=context_id)
    if metadata:
        task.metadata.update(metadata)
    if recall:
        task.metadata[RECALL_KEY] = recall
    return await handler.handle_task(task)


class CliTransport:
    def __init__(self, *, prompt: str = "you> ", reply_prefix: str = "agent> "):
        self.prompt = prompt
        self.reply_prefix = reply_prefix

    async def serve(self, handler: Handler) -> None:
        task = Task(context_id="cli")
        print("(interactive chat — 'exit' or Ctrl-D to quit; pastes are one message)")
        while True:
            line = await asyncio.to_thread(read_message, self.prompt)
            if line is None:
                print()
                return
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
