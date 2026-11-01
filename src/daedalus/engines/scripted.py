"""A model-free engine for testing the plumbing (issue #227).

Each message is one command, run through the same seams a model-driven turn uses:

    remember: <fact> [#tag]   MemoryStore.commit, with the #words as its tags
    recall: <query>           the store's hits as a JSON list of {"content", "source"},
                              from recall_hits when the store has it
    call: <tool> <json args>  turn.call_tool, so the allowlist, approval gates and
                              tool provider decide, exactly as for the litellm engine

Available only when DAEDALUS_SCRIPTED_ENGINE=1 (daedalus.agent.engines), like cabal's
tck persona: an agent that answers by script must never be one an operator picks by
accident. The model is never called, so a bundle's model.id can be a placeholder.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from daedalus.task import ReasonCode, TaskState

if TYPE_CHECKING:
    from daedalus.engines.base import EngineTurn
    from daedalus.task import Task

log = logging.getLogger(__name__)

ENV = "DAEDALUS_SCRIPTED_ENGINE"
COMMANDS = ("remember", "recall", "call")


class ScriptedEngine:
    name = "scripted"

    async def run(self, turn: EngineTurn) -> Task:
        task = turn.task
        verb, _, rest = task.user_text().strip().partition(":")
        verb, rest = verb.strip().lower(), rest.strip()
        turn.emit("scripted_command", command=verb)
        if verb not in COMMANDS or not rest:
            return _refuse(task, f"expected one of {', '.join(c + ': ...' for c in COMMANDS)}")
        if verb == "call":
            name, _, raw = rest.partition(" ")
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                return _refuse(task, f"call: arguments aren't JSON: {exc}")
            if not isinstance(args, dict):
                return _refuse(task, "call: arguments must be a JSON object")
            return task.reply(await turn.call_tool(name, args))
        if turn.memory is None:
            return _refuse(task, f"{verb}: this agent has no memory store")
        if verb == "remember":
            words = rest.split()
            tags = ",".join(w[1:] for w in words if w.startswith("#") and len(w) > 1)
            fact = " ".join(w for w in words if not (w.startswith("#") and len(w) > 1))
            if not fact:
                return _refuse(task, "remember: nothing to remember besides tags")
            await turn.memory.commit(fact, tags=tags)
            return task.reply(f"remembered: {fact}" + (f" [{tags}]" if tags else ""))
        recall_hits = getattr(turn.memory, "recall_hits", None)
        if recall_hits is not None:
            hits = await recall_hits(rest)
        else:
            hits = [{"content": c, "source": "local"} for c in await turn.memory.recall(rest)]
        return task.reply(json.dumps(hits))


def _refuse(task: Task, why: str) -> Task:
    log.debug("scripted: %s", why)
    return task.reply(f"scripted: {why}", state=TaskState.FAILED, reason=ReasonCode.ESCALATION)
