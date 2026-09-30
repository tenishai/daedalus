"""The published bundle shape — daedalus formally controls it.

docs/bundle.schema.json is the contract non-Python consumers (cabal's
bundle reader, editors) validate against. The drift guard pins it to the
runtime's own emission: if AgentSpec changes, this fails until the artifact
is regenerated (`daedalus schema --out docs/bundle.schema.json`).
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from daedalus.bundle import _PLACEHOLDER_PATTERN, bundle_schema
from daedalus.cli import main

ARTIFACT = Path(__file__).parents[1] / "docs" / "bundle.schema.json"


def test_committed_schema_cannot_drift_from_the_runtime():
    committed = json.loads(ARTIFACT.read_text())
    assert committed == bundle_schema(), (
        "docs/bundle.schema.json is stale — regenerate with "
        "`daedalus schema --out docs/bundle.schema.json`"
    )


def test_schema_covers_the_spec_surface():
    schema = bundle_schema()
    for field in (
        "name",
        "card",
        "persona",
        "model",
        "transports",
        "tools",
        "memory",
        "checkpoints",
        "approvals",
    ):
        assert field in schema["properties"], f"missing top-level field: {field}"
    assert schema["version"]
    assert schema["$id"].endswith("bundle.schema.json")
    # transports carry kind-specific extras (port, channel, auth) — the
    # schema must not close them off
    transport = schema["$defs"]["TransportSpec"]
    assert transport.get("additionalProperties") is not False


def test_a_transport_entry_needs_nothing_but_its_kind():
    """`- kind: slack` with no channel is a valid entry (2026-09-23): the
    allowlist is optional, and an absent one means every channel the app was
    invited to. Consumers validating against the artifact must accept it."""
    transport = bundle_schema()["$defs"]["TransportSpec"]
    assert transport["required"] == ["kind"]


def test_the_loader_accepts_a_slack_block_with_no_channel(tmp_path):
    from daedalus.bundle import Bundle

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntransports:\n- kind: slack\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    spec = Bundle.load(tmp_path).spec
    assert spec.transports[0].kind == "slack"
    assert spec.transports[0].options() == {}


def test_cli_schema_prints_valid_json():
    result = CliRunner().invoke(main, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "daedalus agent bundle (agent.yaml)"


def test_a_placeholder_matches_the_pattern_the_loader_actually_expands():
    """bundle_schema()'s placeholder branch must accept exactly what
    daedalus.bundle.interpolate() expands (tenishai/daedalus#130) — checked
    against real examples rather than assumed to match the source regex."""
    for text in ("${PRUDENCE_MODEL}", "${PRUDENCE_MAX_TOKENS:-16384}", "${X:-}"):
        assert re.fullmatch(_PLACEHOLDER_PATTERN, text), text
    for text in ("16384", "${1NOPE}", "not-a-var", "${OK}-suffix", "prefix-${OK}"):
        assert not re.fullmatch(_PLACEHOLDER_PATTERN, text), text


def test_a_placeholder_is_accepted_for_every_non_string_scalar_field():
    """Every int/bool/enum field also accepts a bare placeholder string, mirroring
    the loader's own expansion (#130) — walk every field in the published schema,
    not just the five fields issue #130 measured."""
    schema = bundle_schema()

    def leaf_types(node: dict) -> set[str]:
        # An enum branch also carries "type": "string", so check "enum" first, per branch.
        types = set()
        for branch in node.get("anyOf", [node]):
            if "enum" in branch:
                types.add("enum")
            elif "type" in branch:
                types.add(branch["type"])
        return types

    checked = 0
    enums = 0
    for defn in [schema, *schema.get("$defs", {}).values()]:
        for name, field_schema in defn.get("properties", {}).items():
            types = leaf_types(field_schema)
            if types & {"integer", "number", "boolean", "enum"}:
                branches = field_schema.get("anyOf", [])
                placeholder = [b for b in branches if b.get("pattern") == _PLACEHOLDER_PATTERN]
                assert placeholder, f"{name}: no placeholder branch ({field_schema})"
                checked += 1
                enums += "enum" in types
    assert checked >= 5  # at least the five fields issue #130 measured
    assert enums >= 2  # memory.personal and tools.on_exhausted, from issue #130's five


def test_bundles_with_placeholders_in_non_string_fields_validate(tmp_path):
    """The exact five failures issue #130 measured against main 61fed2d, raw
    (unexpanded) against the published schema (jsonschema, a dev dependency)."""
    import jsonschema
    import yaml

    raw = yaml.safe_load("""
name: prudence
model:
  id: "${PRUDENCE_MODEL:-best}"
  max_tokens: "${PRUDENCE_MAX_TOKENS:-16384}"
memory:
  personal: "${PRUDENCE_MEMORY:-none}"
tools:
  max_steps: "${PRUDENCE_MAX_STEPS:-80}"
  context_tokens: "${PRUDENCE_CONTEXT_TOKENS:-1000000000}"
  on_exhausted: "${PRUDENCE_ON_EXHAUSTED:-wrap_up}"
""")
    jsonschema.validate(raw, bundle_schema())


def test_schema_refuses_removed_model_tier_fields():
    # #129 removed model.tier and model.min_tier; load refuses them, so the published
    # schema must too, or a consumer validating against it (kobo) accepts a bundle
    # that daedalus won't load.
    import jsonschema

    schema = bundle_schema()
    for extra in ({"tier": "best"}, {"min_tier": "fast"}):
        bad = {"name": "x", "model": {"id": "best", **extra}}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
    jsonschema.validate({"name": "x", "model": {"id": "best"}}, schema)
