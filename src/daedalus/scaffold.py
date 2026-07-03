"""Templates for `daedalus create` — the smallest honest bundle."""

from __future__ import annotations

import json
from pathlib import Path

_AGENT_YAML = """\
name: __NAME__
card: agent-card.json
persona: persona.md

model:
  id: ${DAEDALUS_MODEL:-anthropic/claude-sonnet-4-5}
  max_tokens: 4096

transports:
  - kind: cli

tools:
  provider: none

memory:
  personal: none

approvals:
  escalation:
    route: cli
"""

_PERSONA = """\
# __NAME__

You are __NAME__. Describe here who this agent is, what it is responsible
for, and how it should behave. This file is the single persona source —
every transport uses it.
"""


def create_bundle(name: str, parent: str | Path = ".") -> Path:
    root = Path(parent) / name
    if root.exists():
        raise FileExistsError(f"{root} already exists")
    root.mkdir(parents=True)

    card = {
        "name": name,
        "description": f"{name} — describe what this agent does.",
        "version": "0.1.0",
        "skills": [],
    }
    (root / "agent-card.json").write_text(json.dumps(card, indent=2) + "\n")
    (root / "persona.md").write_text(_PERSONA.replace("__NAME__", name))
    (root / "agent.yaml").write_text(_AGENT_YAML.replace("__NAME__", name))
    return root
