"""Engines run a task's turn: the model plus the agent loop (tenishai/agents#51).

The bundle says what the agent is; the engine is what runs it, chosen by the operator
(daedalus run --engine, or DAEDALUS_ENGINE; daedalus.agent.ENGINES lists them): litellm, the
default (engines/litellm.py), or claude-code (engines/claude_code.py).
"""

from daedalus.engines.base import Engine, EngineTurn

__all__ = ["Engine", "EngineTurn"]
