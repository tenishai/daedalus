"""`daedalus guide` and the guides the wheel carries.

The failure these guard is silent: a guide left out of pyproject's force-include
still prints from a checkout (the loader falls back to docs/), so a developer sees
it work while an installed daedalus has no such file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from daedalus.cli import main
from daedalus.docs import MANIFEST, guide_path, read_guide

REPO = Path(__file__).resolve().parent.parent


def _force_included() -> dict[str, str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})


def test_every_guide_ships_in_the_wheel():
    shipped = {
        Path(dst).stem
        for dst in _force_included().values()
        if dst.startswith("daedalus/docs/") and dst.endswith(".md")
    }
    assert shipped, "no guides in the wheel's force-include table"
    assert set(MANIFEST) == shipped


@pytest.mark.parametrize("src", sorted(_force_included()))
def test_every_force_included_file_exists(src):
    assert (REPO / src).is_file(), f"force-include names {src}, which does not exist"


def test_every_guide_reads():
    for name in MANIFEST:
        assert len(read_guide(name)) > 1000, name


def test_a_guide_name_cannot_leave_the_guide_directory():
    for hostile in ("../../../etc/passwd", "/etc/passwd", "../pyproject", "..", ""):
        with pytest.raises(KeyError):
            guide_path(hostile)


def test_guide_with_no_name_lists_every_guide():
    result = CliRunner().invoke(main, ["guide"])
    assert result.exit_code == 0, result.output
    for name, line in MANIFEST.items():
        assert name in result.output
        assert line in result.output


def test_guide_prints_the_welcome_guide():
    result = CliRunner().invoke(main, ["guide", "welcome"])
    assert result.exit_code == 0, result.output
    assert result.output == read_guide("welcome")


def test_an_unknown_guide_fails_and_names_the_valid_ones():
    result = CliRunner().invoke(main, ["guide", "nope"])
    assert result.exit_code == 1
    for name in MANIFEST:
        assert name in result.output


def test_the_schema_tells_an_author_which_files_roots_are_refused():
    import json

    schema = json.loads((REPO / "docs" / "bundle.schema.json").read_text(encoding="utf-8"))
    text = schema["$defs"]["ToolsSpec"]["properties"]["builtin"]["description"]
    for needle in ("/proc", "/etc", "$HOME", "$TECHNE_HOME"):
        assert needle in text


def test_the_image_build_context_keeps_every_shipped_guide():
    """docker/Dockerfile builds the wheel from `.`; a guide that .dockerignore drops
    fails the force-include and breaks the image build (PR 223 review)."""
    import fnmatch

    root = Path(__file__).resolve().parent.parent
    sources = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"][
        "targets"
    ]["wheel"]["force-include"]
    rules = [
        line.strip()
        for line in (root / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for src in sources:
        kept = True
        for rule in rules:  # last match wins, as in Docker
            neg = rule.startswith("!")
            pat = rule.lstrip("!").rstrip("/")
            if fnmatch.fnmatch(src, pat) or src.startswith(pat + "/"):
                kept = neg
        assert kept, f"{src} is force-included but .dockerignore drops it"
