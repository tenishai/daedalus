"""Engines run a task's turn: the model plus the agent loop (tenishai/agents#51).

The bundle says what the agent is; the engine is what runs it, chosen by the operator.
Only the LiteLLM engine exists so far.
"""

from daedalus.engines.base import Engine, EngineTurn

__all__ = ["Engine", "EngineTurn"]
