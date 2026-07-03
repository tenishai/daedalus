"""The agent bundle — a directory that IS an agent.

agent-card.json (A2A identity) + persona.md + agent.yaml (wiring).
The GUI reads and writes the same files; no code generation.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_persona_comments(persona: str) -> str:
    """Remove HTML comments — author-facing notes never reach the model.

    A persona file is a prompt: everything else in it ships verbatim.
    """
    return _HTML_COMMENT.sub("", persona).strip() + "\n"


class BundleError(Exception):
    """The bundle is missing, malformed, or references undefined variables."""


def interpolate(value, env: dict[str, str] | None = None):
    """Expand ${VAR} / ${VAR:-default} in strings, recursively through dicts/lists."""
    env = os.environ if env is None else env
    if isinstance(value, str):

        def _sub(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise BundleError(f"undefined variable ${{{name}}} (no default given)")

        return _VAR.sub(_sub, value)
    if isinstance(value, dict):
        return {key: interpolate(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, env) for item in value]
    return value


class _WarnExtra(BaseModel):
    """Unknown fields load fine (forward-compat with newer writers) but warn."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _warn_on_extras(self):
        if self.__pydantic_extra__:
            keys = ", ".join(sorted(self.__pydantic_extra__))
            warnings.warn(
                f"{type(self).__name__}: unknown fields ignored by this runtime: {keys}",
                stacklevel=2,
            )
        return self


class BudgetSpec(BaseModel):
    per_task_tokens: int | None = None


class ModelSpec(BaseModel):
    id: str
    max_tokens: int = 8192
    api_base: str | None = None  # OpenAI-compatible endpoints (vLLM, ollama, LM Studio)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


class TransportSpec(BaseModel):
    """kind plus transport-specific fields (channel, port, ...) kept as extras."""

    model_config = ConfigDict(extra="allow")

    kind: str

    def options(self) -> dict:
        return dict(self.__pydantic_extra__ or {})


class ToolsSpec(BaseModel):
    provider: Literal["none", "mcp", "limenal"] = "none"
    servers: list[dict] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)


class MemorySpec(BaseModel):
    personal: Literal["none", "techne"] = "none"
    team: Literal["none", "outbox"] = "none"


class CeilingSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    write: bool | None = None
    hosts: list[str] | None = None


class GateSpec(BaseModel):
    match: str
    require: str = "plan_approval"


class EscalationSpec(BaseModel):
    route: str = "cli"


class ApprovalsSpec(BaseModel):
    ceiling: CeilingSpec = Field(default_factory=CeilingSpec)
    gates: list[GateSpec] = Field(default_factory=list)
    escalation: EscalationSpec = Field(default_factory=EscalationSpec)


class AgentSpec(_WarnExtra):
    """Schema of agent.yaml. Approvals are parsed in v0.1, enforced from v0.2."""

    name: str
    card: str = "agent-card.json"
    persona: str = "persona.md"
    model: ModelSpec
    transports: list[TransportSpec] = Field(default_factory=lambda: [TransportSpec(kind="cli")])
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    approvals: ApprovalsSpec = Field(default_factory=ApprovalsSpec)


@dataclass
class Bundle:
    path: Path
    spec: AgentSpec
    card: dict
    persona: str

    @classmethod
    def load(cls, path: str | Path, env: dict[str, str] | None = None) -> Bundle:
        root = Path(path)
        wiring = root / "agent.yaml"
        if not wiring.is_file():
            raise BundleError(f"not an agent bundle: {wiring} does not exist")

        raw = yaml.safe_load(wiring.read_text()) or {}
        if not isinstance(raw, dict):
            raise BundleError(f"{wiring}: expected a mapping at top level")
        spec = AgentSpec.model_validate(interpolate(raw, env))

        card_file = root / spec.card
        persona_file = root / spec.persona
        for required in (card_file, persona_file):
            if not required.is_file():
                raise BundleError(f"bundle references missing file: {required}")

        try:
            card = json.loads(card_file.read_text())
        except json.JSONDecodeError as exc:
            raise BundleError(f"{card_file}: invalid JSON — {exc}") from exc

        return cls(
            path=root,
            spec=spec,
            card=card,
            persona=strip_persona_comments(persona_file.read_text()),
        )
