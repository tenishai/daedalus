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


class BudgetSpec(_WarnExtra):
    per_task_tokens: int | None = None


class ModelSpec(_WarnExtra):
    id: str
    max_tokens: int = 8192
    api_base: str | None = None  # OpenAI-compatible endpoints (vLLM, ollama, LM Studio)
    api_key_env: str | None = None  # env var holding a real key; never the key itself
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


class TransportSpec(BaseModel):
    """kind plus transport-specific fields (channel, port, ...) kept as extras."""

    model_config = ConfigDict(extra="allow")

    kind: str

    def options(self) -> dict:
        return dict(self.__pydantic_extra__ or {})


class LiminalSpec(_WarnExtra):
    url: str | None = None  # e.g. http://localhost:9100
    token_env: str | None = None  # env var holding the bearer token; unset = no auth


class ColleaguesSpec(_WarnExtra):
    url: str | None = None  # switchboard directory base URL
    token_env: str | None = None  # env var holding the bearer token; unset/empty = degraded
    # Deliberately named `self`: Pydantic v2 model fields never collide with a
    # method's self (BaseModel builds __init__ from the schema, not the name).
    self: str | None = None  # this agent's own name, excluded from directory listings


class ToolsSpec(_WarnExtra):
    provider: Literal["none", "mcp", "liminal", "colleagues"] = "none"
    servers: list[dict] = Field(default_factory=list)
    liminal: LiminalSpec = Field(default_factory=LiminalSpec)
    colleagues: ColleaguesSpec = Field(default_factory=ColleaguesSpec)
    builtin: dict[str, dict] = Field(default_factory=dict)  # e.g. files: {roots: [...]}
    allow: list[str] = Field(default_factory=list)  # ceiling; empty = deny everything
    max_steps: int = 10


class MemorySpec(_WarnExtra):
    personal: Literal["none", "techne"] = "none"
    home: str | None = None  # techne home dir; default is techne's own (~/.techne)
    team: Literal["none", "outbox"] = "none"


class CeilingSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    write: bool | None = None
    hosts: list[str] | None = None


class GateSpec(_WarnExtra):
    match: str
    require: str = "plan_approval"


class EscalationSpec(_WarnExtra):
    route: str = "cli"


class ApprovalsSpec(_WarnExtra):
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
    checkpoints: Literal["none", "sqlite"] = "none"
    approvals: ApprovalsSpec = Field(default_factory=ApprovalsSpec)


def bundle_schema() -> dict:
    """The canonical agent.yaml shape as JSON Schema — the published contract
    for consumers that can't import Python (cabal's bundle reader, editors).

    The runtime is the source of truth: this is emitted from AgentSpec, and
    a test pins docs/bundle.schema.json to this output so the committed
    artifact cannot drift. Regenerate with `daedalus schema`.
    """
    import daedalus  # function-local: daedalus/__init__ imports this module

    return {
        **AgentSpec.model_json_schema(),
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/tenishai/daedalus/blob/main/docs/bundle.schema.json",
        "title": "daedalus agent bundle (agent.yaml)",
        "version": daedalus.__version__,
        "description": (
            "Wiring for a daedalus agent bundle — a directory that IS an "
            "agent (agent.yaml + persona.md + agent-card.json). Unknown "
            "fields load with a warning, never silently."
        ),
    }


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
