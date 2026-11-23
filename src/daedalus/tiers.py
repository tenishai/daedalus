"""Model tiers (tenishai/daedalus#103, #129): a bundle says how much model it needs,
and each deployment says which model fills that need.

`model.id` is either a concrete model id, or one of the tier words `fast`, `balanced`,
`best` (after `${VAR}` expansion). A tier word is looked up in a YAML file named by
DAEDALUS_TIERS, mapping tiers to models once per deployment:

    tiers:
      best:     {engine: claude-code, id: claude-opus-5-5}
      balanced: {engine: claude-code, id: claude-sonnet-5-5}
      fast:
        engine: litellm
        id: openai/local-model
        api_base: http://localhost:8000/v1
        api_key_env: LOCAL_API_KEY
        context_tokens: 45000      # this model on this hardware
        request_timeout: 300       # seconds per model call; a slow local model
        max_repeats: 3
        max_tokens: 16384          # the visible reply's ceiling (bundle default 8192)
        thinking: {mode: off}      # or {mode: on, budget_tokens: 4096}; see daedalus.llm
      # a tier can alias another (balanced: best), or be null: not offered here

Resolution rules (`daedalus.agent._resolve_model`):
- A concrete `model.id` is only recorded.
- A tier word is looked up in DAEDALUS_TIERS; a deployment that doesn't offer it
  refuses to load.
- Limits that belong to a model (context_tokens, max_repeats, max_tokens,
  request_timeout, thinking, sampling, sampling_by_thinking) come from the tier entry
  only where the bundle hasn't set them. This is where a deployment raises the reply
  ceiling or sets the thinking budget for every bundle on a tier (tenishai/daedalus#240):
  the bundle default stays 8192.
- The resolved model is always recorded, because a tier floats the way an alias does:
  which tier, which engine, which id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from daedalus.bundle import SamplingSpec, ThinkingSpec, _yaml_bool_keys

TIER_NAMES: tuple[str, ...] = ("fast", "balanced", "best")  # lowest to highest
Tier = Literal["fast", "balanced", "best"]
TIERS_ENV = "DAEDALUS_TIERS"


class TierError(ValueError):
    """A tier could not be resolved; the message says what to fix."""


class TierEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: Literal["litellm", "claude-code"] = "litellm"
    id: str
    api_base: str | None = None
    api_key_env: str | None = None  # env var holding the key; never the key itself
    max_tokens: int | None = Field(default=None, gt=0)
    thinking: ThinkingSpec | None = None  # fills model.thinking when the bundle is silent
    request_timeout: float | None = Field(default=None, gt=0)  # seconds per model call
    context_tokens: int | None = Field(default=None, gt=0)
    max_repeats: int | None = Field(default=None, ge=2)
    # Fill model.sampling / model.sampling_by_thinking when the bundle is silent.
    sampling: SamplingSpec | None = None
    sampling_by_thinking: dict[Literal["on", "off"], SamplingSpec] | None = None

    @field_validator("sampling_by_thinking", mode="before")
    @classmethod
    def _sampling_keys_are_words(cls, value):
        return _yaml_bool_keys(value)

    @model_validator(mode="after")
    def _sampling_needs_litellm(self):
        # Claude Code runs its own model calls, so sampling here would do nothing.
        if self.engine == "claude-code" and (self.sampling or self.sampling_by_thinking):
            raise ValueError("sampling is for litellm tiers; the claude-code engine ignores it")
        return self


@dataclass(frozen=True)
class Resolution:
    """What a tier resolved to, for the trace, the log and the agent card."""

    tier: str | None  # the tier word model.id named (None: it was a concrete id)
    used: str | None  # the tier actually used
    engine: str
    id: str
    source: str  # "id" (the bundle pinned a concrete model) or the tiers file's path

    @property
    def fell_back(self) -> bool:
        # Kept for the card/event shape (tenishai/daedalus#103): daedalus no longer
        # has a fallback mechanism (model.min_tier was removed, #129), so `used`
        # always equals `tier` once resolved and this is always False. A pinned id
        # has used=None, which is a pin, not a fallback.
        return self.used is not None and self.used != self.tier

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "used": self.used,
            "engine": self.engine,
            "id": self.id,
            "fell_back": self.fell_back,
            "source": self.source,
        }


def load_tiers(path: str | Path) -> dict[str, TierEntry | None]:
    """Read a tiers file: each tier maps to an entry, another tier's name, or null."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except OSError as exc:
        raise TierError(f"can't read the tiers file {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TierError(f"{p}: not valid YAML: {exc}") from exc
    table = raw.get("tiers") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        raise TierError(f"{p}: expected a top-level 'tiers:' mapping")
    unknown = sorted(set(table) - set(TIER_NAMES))
    if unknown:
        raise TierError(
            f"{p}: unknown tier(s) {', '.join(unknown)}; tiers are {', '.join(TIER_NAMES)}"
        )

    def entry(name: str, seen: tuple[str, ...] = ()) -> TierEntry | None:
        value = table.get(name)
        if value is None:
            return None
        if isinstance(value, str):  # an alias to another tier
            if value not in TIER_NAMES:
                raise TierError(f"{p}: tier {name} aliases unknown tier {value!r}")
            if value in seen or value == name:
                raise TierError(f"{p}: tier aliases loop: {' -> '.join((*seen, name, value))}")
            return entry(value, (*seen, name))
        if not isinstance(value, dict):
            raise TierError(f"{p}: tier {name} must be a mapping, another tier's name, or null")
        try:
            return TierEntry.model_validate(value)
        except ValidationError as exc:
            raise TierError(
                f"{p}: tier {name}: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})"
            ) from exc

    return {name: entry(name) for name in TIER_NAMES}


def tiers_path(env: dict[str, str] | None = None) -> Path | None:
    value = (env if env is not None else os.environ).get(TIERS_ENV, "").strip()
    return Path(value).expanduser() if value else None


def pick(tier: str, table: dict[str, TierEntry | None]) -> TierEntry:
    """The tiers-file entry for `tier`, or a TierError naming what's offered instead.

    No fallback (tenishai/daedalus#129 removed model.min_tier): a tier the deployment
    doesn't offer refuses to load rather than running on a lower one.
    """
    found = table.get(tier)
    if found is not None:
        return found
    offered = [n for n in TIER_NAMES if table.get(n) is not None]
    raise TierError(
        f"this bundle needs the {tier} tier, and this deployment offers "
        f"{', '.join(offered) or 'none'}; map it in the tiers file"
    )
