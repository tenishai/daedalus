"""Model tiers (tenishai/daedalus#103): a bundle says how much model it needs, and each
deployment says which model fills that need.

A bundle declares `model.tier: fast | balanced | best` instead of (or beside) a
`model.id`. The operator maps tiers to models once per deployment, in a YAML file named
by DAEDALUS_TIERS:

    tiers:
      best:     {engine: claude-code, id: claude-opus-5-5}
      balanced: {engine: claude-code, id: claude-sonnet-5-5}
      fast:
        engine: litellm
        id: openai/mtplx-qwen35-9b-optimized-speed
        api_base: http://192.168.10.202:8000/v1
        api_key_env: MTPLX_API_KEY
        context_tokens: 45000      # this model on this hardware
        max_repeats: 3
      # a tier can alias another (balanced: best), or be null: not available here

Resolution rules:
- An explicit, non-empty `model.id` wins; the tier is then only recorded.
- A tier the deployment doesn't offer refuses to load, unless the bundle names
  `model.min_tier`: then the next tier down is used, as far as min_tier, and the
  fallback is recorded.
- Limits that belong to a model (context_tokens, max_repeats, max_tokens) come from
  the tier entry only where the bundle hasn't set them.
- The resolved model is always recorded, because a tier floats the way an alias does
  (agents-archive#142): which tier, which engine, which id, and whether it fell back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    context_tokens: int | None = Field(default=None, gt=0)
    max_repeats: int | None = Field(default=None, ge=2)


def tiers_schema() -> dict:
    """The tiers file's shape as JSON Schema — the published contract for the
    file DAEDALUS_TIERS names (cabal's bundle reader, editors, an operator
    hand-writing a deployment's tiers.yaml).

    A top-level `tiers` object with optional keys fast/balanced/best; each
    value is a TierEntry, a string naming another tier (an alias), or null
    (not offered here). A test pins docs/tiers.schema.json to this output,
    mirroring bundle_schema()'s drift guard. Regenerate with
    `daedalus schema --tiers --out docs/tiers.schema.json`.
    """
    import daedalus  # function-local: daedalus/__init__ imports this module

    entry_schema = TierEntry.model_json_schema()
    defs = entry_schema.pop("$defs", {})
    defs["TierEntry"] = entry_schema
    tier_value = {
        "oneOf": [
            {"$ref": "#/$defs/TierEntry"},
            {"type": "string", "enum": list(TIER_NAMES)},
            {"type": "null"},
        ]
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/tenishai/daedalus/blob/main/docs/tiers.schema.json",
        "title": "daedalus tiers file (tiers.yaml)",
        "version": daedalus.__version__,
        "description": (
            "A deployment's tier -> model map, named by DAEDALUS_TIERS (daedalus.tiers, "
            "tenishai/daedalus#103). Each of fast/balanced/best is a TierEntry, another "
            "tier's name (an alias), or null (not offered here)."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": ["tiers"],
        "properties": {
            "tiers": {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: tier_value for name in TIER_NAMES},
            }
        },
        "$defs": defs,
    }


@dataclass(frozen=True)
class Resolution:
    """What a tier resolved to, for the trace, the log and the agent card."""

    tier: str | None  # the tier the bundle asked for (None: it pinned an id)
    used: str | None  # the tier actually used (differs from `tier` after a fallback)
    engine: str
    id: str
    source: str  # "id" (the bundle pinned it) or the tiers file's path

    @property
    def fell_back(self) -> bool:
        # A pinned id has used=None: that is a pin, not a fallback (Prudence, PR 127).
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


def pick(
    tier: str, min_tier: str | None, table: dict[str, TierEntry | None]
) -> tuple[str, TierEntry]:
    """The tier to use: the one asked for, else lower ones down to min_tier."""
    floor = min_tier or tier
    if TIER_NAMES.index(floor) > TIER_NAMES.index(tier):
        raise TierError(f"model.min_tier {floor} is above model.tier {tier}")
    for name in reversed(TIER_NAMES[TIER_NAMES.index(floor) : TIER_NAMES.index(tier) + 1]):
        found = table.get(name)
        if found is not None:
            return name, found
    offered = [n for n in TIER_NAMES if table.get(n) is not None]
    wanted = tier if floor == tier else f"{tier} (or down to {floor})"
    raise TierError(
        f"this bundle needs the {wanted} tier, and this deployment offers "
        f"{', '.join(offered) or 'none'}; map it in the tiers file, or set model.min_tier "
        "if the bundle can run on less"
    )
