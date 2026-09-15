"""skills.load — fetch an assigned skill's body by name.

Progressive disclosure, not prompt stuffing. The system prompt carries an index
of one line per assigned skill (`Agent._skills_index`); the bodies stay here and
are fetched on demand. A fourteen-skill catalogue is ~183 KB and grows, so
concatenating bodies into the persona works at three skills and falls over at
thirty.

This closes the gap named in #58: the boot seeder already puts every SKILL.md
into the agent's memory, which makes a skill RECALLABLE, but recall is
probabilistic and task-shaped. A persona saying "start with <skill>" is a
deterministic request, and nothing guaranteed the right body surfaced for it.
"""

from __future__ import annotations

from daedalus.bundle import Skill


class SkillsToolProvider:
    """One tool, `skills.load(name)`, over the bundle's parsed skills."""

    def __init__(self, skills: dict[str, Skill]):
        self.skills = skills

    async def tools(self) -> list[dict]:
        # The known ids go in the enum rather than only the description: the
        # model cannot then invent a plausible-sounding skill name, and the
        # listing itself documents what is assigned.
        return [
            {
                "name": "skills.load",
                "description": (
                    "Load the full text of one assigned skill by name. "
                    "Use when a persona or task names a skill, or when the "
                    "skills index says one applies."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The skill id, as listed in the skills index.",
                            "enum": sorted(self.skills),
                        }
                    },
                    "required": ["name"],
                },
            }
        ]

    async def call(self, name: str, payload: dict) -> str:
        if name != "skills.load":
            raise LookupError(f"unknown tool {name!r}")
        requested = payload.get("name")
        if not requested:
            raise ValueError("'name' is required")
        skill = self.skills.get(requested)
        if skill is None:
            # Name the alternatives. A model that guessed wrong can correct
            # itself in one step instead of guessing again.
            known = ", ".join(sorted(self.skills)) or "none assigned"
            raise LookupError(f"no skill named {requested!r} (assigned: {known})")
        return skill.body
