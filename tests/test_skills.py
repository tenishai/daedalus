"""Skills are addressable, not merely recallable (#58) and unmet tool
preconditions are reported rather than silent (#59)."""

import json
import logging
import shutil
import warnings
from pathlib import Path

import pytest

from daedalus.agent import Agent
from daedalus.bundle import Bundle, load_skills, parse_skill, unmet_requirements
from daedalus.tools.skills import SkillsToolProvider

GOLDEN = Path(__file__).parent / "golden" / "greeter"

COST_REPORT = """\
---
name: cost-report
when_to_use: When asked for monthly spend, before touching the console.
requires: [gateway.logs, datadog.query]
version: 1.2.0
---
# Cost report

Run the monthly cost query and post it.
"""


def _bundle_with_skills(tmp_path, skills: dict[str, str], allow=("skills.load", "gateway.*")):
    root = tmp_path / "agent"
    shutil.copytree(GOLDEN, root)
    wiring = (root / "agent.yaml").read_text()
    wiring += "\ntools:\n  provider: none\n  allow:\n"
    wiring += "".join(f"    - {pattern}\n" for pattern in allow)
    (root / "agent.yaml").write_text(wiring)
    for skill_id, text in skills.items():
        (root / "skills" / skill_id).mkdir(parents=True)
        (root / "skills" / skill_id / "SKILL.md").write_text(text)
    return root


# --- parsing -----------------------------------------------------------------


def test_frontmatter_is_parsed_and_removed_from_the_body(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(COST_REPORT)
    skill = parse_skill("cost-report", path)

    assert skill.name == "cost-report"
    assert skill.when_to_use.startswith("When asked for monthly spend")
    assert skill.requires == ["gateway.logs", "datadog.query"]
    assert skill.meta["version"] == "1.2.0"
    assert skill.body.startswith("# Cost report")
    assert "requires:" not in skill.body


def test_a_scalar_requires_is_accepted_as_a_one_item_list(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nrequires: gateway.logs\n---\nbody\n")
    assert parse_skill("s", path).requires == ["gateway.logs"]


def test_skill_without_frontmatter_is_all_body(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("Just do the thing.\n")
    skill = parse_skill("plain", path)
    assert skill.body == "Just do the thing."
    assert skill.requires == [] and skill.when_to_use is None
    # Still addressable: the index falls back to the id rather than omitting it.
    assert skill.summary == "plain"


def test_unparseable_frontmatter_warns_and_degrades_rather_than_raising(tmp_path):
    """A catalog-metadata typo must not stop an agent booting — the body is
    still a usable procedure."""
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: [unclosed\n---\nthe body\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        skill = parse_skill("broken", path)
    assert skill.body == "the body"
    assert skill.meta == {}
    assert any("frontmatter" in str(w.message) for w in caught)


def test_load_skills_uses_the_same_one_level_glob_as_the_seeder(tmp_path):
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("A")
    (tmp_path / "skills" / "b" / "nested").mkdir(parents=True)
    (tmp_path / "skills" / "b" / "nested" / "SKILL.md").write_text("too deep")
    (tmp_path / "skills" / "c").mkdir()
    (tmp_path / "skills" / "c" / "README.md").write_text("not a skill")
    assert sorted(load_skills(tmp_path)) == ["a"]


# --- Bundle integration ------------------------------------------------------


def test_bundle_carries_its_skills(tmp_path):
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    bundle = Bundle.load(root)
    assert list(bundle.skills) == ["cost-report"]
    assert bundle.skills["cost-report"].requires == ["gateway.logs", "datadog.query"]


def test_a_bundle_with_no_skills_dir_has_no_skills(tmp_path):
    bundle = Bundle.load(GOLDEN)
    assert bundle.skills == {}


# --- requires: preconditions (#59) -------------------------------------------


def test_allow_patterns_are_globbed_not_compared_literally(tmp_path):
    """`tools.allow` holds fnmatch PATTERNS. Comparing raw strings would report
    every globbed allowlist as entirely unmet."""
    skills = {"s": parse_skill("s", _write(tmp_path, COST_REPORT))}
    assert unmet_requirements(skills, ["gateway.*", "datadog.query"]) == {}


def test_requirements_the_allowlist_does_not_admit_are_reported(tmp_path):
    skills = {"s": parse_skill("s", _write(tmp_path, COST_REPORT))}
    assert unmet_requirements(skills, ["gateway.*"]) == {"s": ["datadog.query"]}


def test_a_skill_with_no_requires_is_never_unmet(tmp_path):
    skills = {"s": parse_skill("s", _write(tmp_path, "body only\n"))}
    assert unmet_requirements(skills, []) == {}


def test_unmet_requirements_are_logged_at_load(tmp_path, caplog):
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    with caplog.at_level(logging.WARNING):
        Agent.load(root)
    assert any(
        "datadog.query" in r.getMessage() and "cost-report" in r.getMessage()
        for r in caplog.records
    )


def test_skills_present_but_skills_load_not_allowed_is_logged(tmp_path, caplog):
    """The failure this whole feature exists to remove: capabilities assigned,
    none reachable, agent boots green."""
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT}, allow=("gateway.*",))
    with caplog.at_level(logging.WARNING):
        Agent.load(root)
    assert any("skills.load" in r.getMessage() for r in caplog.records)


# --- the index and the tool (#58) --------------------------------------------


def test_index_names_every_skill_and_says_how_to_load_one(tmp_path):
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    agent = Agent.load(root)
    index = agent._skills_index()
    assert "`cost-report`" in index
    assert "When asked for monthly spend" in index
    assert "skills.load" in index


def test_index_carries_summaries_not_bodies(tmp_path):
    """183 KB of bodies cannot go in a system prompt; that is the whole design."""
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    agent = Agent.load(root)
    assert "Run the monthly cost query" not in agent._skills_index()


def test_index_is_suppressed_when_the_tool_is_not_allowed(tmp_path):
    """An index the agent cannot act on is worse than none: it would advertise
    capabilities and leave every one unreachable."""
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT}, allow=("gateway.*",))
    assert Agent.load(root)._skills_index() == ""


@pytest.mark.asyncio
async def test_skills_load_returns_the_body_without_frontmatter(tmp_path):
    skills = {"cost-report": parse_skill("cost-report", _write(tmp_path, COST_REPORT))}
    provider = SkillsToolProvider(skills)
    body = await provider.call("skills.load", {"name": "cost-report"})
    assert body.startswith("# Cost report")
    assert "version:" not in body


@pytest.mark.asyncio
async def test_the_listing_enumerates_assigned_skills(tmp_path):
    skills = {
        "b": parse_skill("b", _write(tmp_path, "b", "b")),
        "a": parse_skill("a", _write(tmp_path, "a", "a")),
    }
    (spec,) = await SkillsToolProvider(skills).tools()
    assert spec["name"] == "skills.load"
    assert spec["parameters"]["properties"]["name"]["enum"] == ["a", "b"]


@pytest.mark.asyncio
async def test_an_unknown_skill_names_the_alternatives(tmp_path):
    skills = {"cost-report": parse_skill("cost-report", _write(tmp_path, COST_REPORT))}
    with pytest.raises(LookupError, match="cost-report"):
        await SkillsToolProvider(skills).call("skills.load", {"name": "cost-repot"})


@pytest.mark.asyncio
async def test_the_tool_is_offered_only_through_the_allowlist(tmp_path):
    """skills.load is fail-closed like every other tool: in the allowlist it is
    offered, out of it the model never sees it."""
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    assert "skills.load" in [s["name"] for s in await Agent.load(root)._allowed_tools()]

    root2 = _bundle_with_skills(
        tmp_path / "second", {"cost-report": COST_REPORT}, allow=("gateway.*",)
    )
    assert "skills.load" not in [s["name"] for s in await Agent.load(root2)._allowed_tools()]


def _write(tmp_path: Path, text: str, stem: str = "SKILL") -> Path:
    """A SKILL.md on disk, since parse_skill reads a path."""
    path = tmp_path / f"{stem}.md"
    path.write_text(text)
    return path


def test_card_is_untouched_by_skills(tmp_path):
    """The A2A card's `skills` array is the advertised identity and is not
    derived from bundle/skills — keep them from silently coupling."""
    root = _bundle_with_skills(tmp_path, {"cost-report": COST_REPORT})
    card = json.loads((root / "agent-card.json").read_text())
    assert card.get("skills", []) == Bundle.load(root).card.get("skills", [])
