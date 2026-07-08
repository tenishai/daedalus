"""The published bundle shape — daedalus formally controls it.

docs/bundle.schema.json is the contract non-Python consumers (cabal's
bundle reader, editors) validate against. The drift guard pins it to the
runtime's own emission: if AgentSpec changes, this fails until the artifact
is regenerated (`daedalus schema --out docs/bundle.schema.json`).
"""

import json
from pathlib import Path

from click.testing import CliRunner

from daedalus.bundle import bundle_schema
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


def test_cli_schema_prints_valid_json():
    result = CliRunner().invoke(main, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "daedalus agent bundle (agent.yaml)"
