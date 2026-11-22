"""The guides an installed daedalus carries, read by `daedalus guide`.

An agent asked "what is this daedalus thing?" should answer from the package on
the machine, not from a repo it may not have. So the guides ship in the wheel:
pyproject.toml's force-include copies each docs/<name>.md into daedalus/docs/ at
build time. A source checkout has no such copy, so the lookup falls back to the
repo's docs/ directory rather than keeping a second copy in the package tree,
which would drift.

`MANIFEST` is the list of guides. tests/test_guides.py fails if it and the
force-include table disagree, because a guide missing from the wheel still works
in a checkout and only breaks for people who installed it.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent

#: name -> one-line description, in the order `daedalus guide` lists them.
MANIFEST: dict[str, str] = {
    "welcome": "What daedalus is, what it can do for you, every option and why you'd pick it.",
    "design": "The design rationale: bundle schema, engines, transports, approvals.",
    "diary": "Seeding a specialist from a transcript diary: build the index, mount it, name it.",
}


def _search_dirs() -> list[Path]:
    """The installed package first, then a source checkout's docs/ directory."""
    dirs = [_DIR]
    repo = _DIR.parent.parent.parent  # src/daedalus/docs -> repo root, in a checkout
    if (repo / "pyproject.toml").is_file():
        dirs.append(repo / "docs")
    return dirs


def guide_path(name: str) -> Path:
    """The file for one guide, or KeyError.

    The name is checked against MANIFEST before any path is built, so a CLI
    argument like ../../etc/passwd never reaches the filesystem."""
    if name not in MANIFEST:
        raise KeyError(name)
    for d in _search_dirs():
        candidate = d / f"{name}.md"
        if candidate.is_file():
            return candidate
    raise KeyError(name)


def read_guide(name: str) -> str:
    return guide_path(name).read_text(encoding="utf-8")
