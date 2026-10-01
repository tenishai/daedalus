"""Conformance: bundles kobo generates and bundles tenishai/agents writes by hand
both pass daedalus's published schema and load (tenishai/daedalus#131).

Two producers write agent.yaml for daedalus: kobo generates one per agent from its
template, and tenishai/agents writes Prudence, Temperance and Constance by hand. They
drift apart without a shared check -- the hand-written bundles failed the published
schema while loading fine (#130), and kobo's own vendored schema had gone stale.

Fixtures live under tests/fixtures/conformance/, one directory per bundle, each
agent.yaml headed with where it was copied from (or how it was rendered) and the
source commit. A minimal persona.md/agent-card.json sits beside each so Bundle.load
has what it needs; a shared tiers.yaml supplies every tier a fixture's model.id
might default to.
"""

from pathlib import Path

import jsonschema
import pytest
import yaml

from daedalus.agent import Agent
from daedalus.bundle import Bundle, bundle_schema
from daedalus.protocols import NoCheckpoints, NoMemory, NoTools
from tests.fakes import ScriptedLLM

FIXTURES = Path(__file__).parent / "fixtures" / "conformance"
TIERS_FILE = FIXTURES / "tiers.yaml"

#: One directory per bundle fixture (everything under FIXTURES except the shared
#: tiers file, which is not itself a bundle).
BUNDLE_DIRS = sorted(p for p in FIXTURES.iterdir() if p.is_dir())

# A real fixture set is the point of this test; an empty one would pass by vacuity.
assert BUNDLE_DIRS, f"no fixture bundles under {FIXTURES}"

#: Every *_MODEL / AGENT_MODEL var a fixture's model.id might read, so "no model env
#: set" is enforced even if the ambient shell happens to export one of these.
_MODEL_ENV_VARS = (
    "PRUDENCE_MODEL",
    "TEMPERANCE_MODEL",
    "CONSTANCE_MODEL",
    "AGENT_MODEL",
    "DAEDALUS_ENGINE",
)


@pytest.mark.parametrize("bundle_dir", BUNDLE_DIRS, ids=lambda p: p.name)
def test_fixture_validates_raw_against_the_published_schema(bundle_dir):
    """As written, placeholders unexpanded -- what a consumer without daedalus
    itself (kobo's own vendored copy, an editor) actually validates against."""
    raw = yaml.safe_load((bundle_dir / "agent.yaml").read_text())
    jsonschema.validate(raw, bundle_schema())


@pytest.mark.parametrize("bundle_dir", BUNDLE_DIRS, ids=lambda p: p.name)
def test_fixture_loads_and_resolves_its_model(bundle_dir, monkeypatch):
    """Bundle.load succeeds, and the model resolves through a fixture tiers file
    with no *_MODEL env set -- every fixture here defaults to a tier word.

    memory/tools/checkpoints are stubbed: this test is about the bundle SHAPE
    daedalus accepts, not about actually reaching techne or an MCP server (Prudence's
    real bundle wants both). `_resolve_model` runs regardless of these overrides --
    it happens before any of them in Agent.__init__.
    """
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DAEDALUS_TIERS", str(TIERS_FILE))

    bundle = Bundle.load(bundle_dir)
    agent = Agent(
        bundle,
        llm=ScriptedLLM("ok"),
        memory=NoMemory(),
        tools=NoTools(),
        checkpoints=NoCheckpoints(),
    )
    assert agent.model_resolution.id
    assert agent.model_resolution.tier in ("fast", "balanced", "best")
