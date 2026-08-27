"""TOOL_MARKER's handler support: the runtime's own copy of kobo's tooling
contract, plus the overlay write path.

Ported here rather than imported across repos (daedalus and kobo are
separate deployables with no shared dependency): the fast lane skips CI by
definition, so this runtime must never trust that kobo's own validation
(`kobo_api.tooling._validate_filename`/`_validate_content`) already ran --
`validate_filename`/`validate_content` below are a byte-for-byte port of
those two functions' rules (filename shape, size cap, UTF-8 decodability,
non-empty, `.py` ast parse, the standard-library-only import allowlist, and
the top-level `pre_task`/`post_draft` entrypoint shape), kept here so a
change on one side is a deliberate, visible edit on the other rather than a
silent drift.

`apply_tool` is the write half: re-validate, then land the file in the
overlay directory on the agent's own persistent volume (beside the techne
db, at `<memory.home>/tools-overlay/`) -- never the image bundle
(`bundle/tools/`), which resets to its baked-in contents on every restart.
An overlay filename that collides with an image-bundle tool is refused here,
at write time; a file with a colliding name planted directly on the overlay
directory's disk (bypassing this function) is the other half of that
guarantee, caught instead at load time by `daedalus.enforcement.
Enforcement.load`.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Byte-for-byte kobo_api.tooling.TOOLING_FILENAME_RE: 1-64 chars, a lowercase
# letter, then lowercase/digit/underscore/hyphen, then ".py" or ".go".
TOOLING_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}\.(py|go)$")

TOOLING_MAX_BYTES = 524288

# The two hook names the enforcement contract calls by name on a tooling
# file -- same pair kobo_api.tooling checks for at upload time.
_ENTRYPOINT_NAMES = ("pre_task", "post_draft")


class ToolingError(Exception):
    """A tool filename/content failed the mechanical tooling contract --
    same failure set as kobo_api.tooling.ToolingError."""


@dataclass
class ToolResult:
    stored: bool
    error: str | None = None
    filename: str = ""


def validate_filename(filename: str) -> None:
    if not TOOLING_FILENAME_RE.fullmatch(filename):
        raise ToolingError(f"invalid tooling filename: {filename!r}")


def _check_import_allowlist(tree: ast.AST) -> None:
    """Walk the whole tree, same as kobo_api.tooling._check_import_allowlist:
    any import beyond the standard library, anywhere in the file, is
    refused; a relative import is refused outright (the flat overlay
    directory has no package structure for one to resolve against)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".")[0]
                if top_level not in sys.stdlib_module_names:
                    raise ToolingError(
                        "tooling file imports beyond the standard library "
                        f"on line {node.lineno}: {top_level}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                raise ToolingError(
                    f"tooling file uses a relative import on line {node.lineno}"
                )
            top_level = (node.module or "").split(".")[0]
            if top_level not in sys.stdlib_module_names:
                raise ToolingError(
                    "tooling file imports beyond the standard library "
                    f"on line {node.lineno}: {top_level}"
                )


def _check_entrypoint(tree: ast.Module) -> None:
    """At least one top-level `def pre_task`/`def post_draft` is required,
    same as kobo_api.tooling._check_entrypoint -- only tree.body (the
    module's own top-level statements) counts, and an async top-level hook
    is rejected outright with its own message."""
    found = False
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in _ENTRYPOINT_NAMES:
            raise ToolingError(
                "tooling file hooks must be regular functions, not async"
            )
        if isinstance(node, ast.FunctionDef) and node.name in _ENTRYPOINT_NAMES:
            found = True
    if not found:
        raise ToolingError("tooling file defines neither pre_task nor post_draft")


def validate_content(filename: str, raw: bytes) -> str:
    """Size cap, UTF-8 decodability, non-empty, and (for a `.py` file) ast
    parse plus the import allowlist and entrypoint checks -- same mechanical
    contract as kobo_api.tooling._validate_content, same order, same
    messages. Returns the decoded text on success.

    Takes raw bytes so a caller reading straight off disk can reuse this
    unchanged; `apply_tool` below (fed `Task.user_text()`, an already-
    decoded str) just `.encode("utf-8")`'s first -- that encode can never
    itself raise, same reasoning kobo_api.tooling's own docstring gives for
    its own str-content caller.
    """
    size = len(raw)
    if size > TOOLING_MAX_BYTES:
        raise ToolingError(
            f"tooling file content is {size} bytes, exceeds the "
            f"{TOOLING_MAX_BYTES}-byte limit"
        )

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolingError("tooling file content is not valid UTF-8 text") from exc

    if not content:
        raise ToolingError("tooling file content must not be empty")

    if filename.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else "unknown"
            raise ToolingError(
                f"tooling file has a syntax error on line {line}: {exc.msg}"
            ) from exc

        _check_import_allowlist(tree)
        _check_entrypoint(tree)

    return content


def apply_tool(content: str, filename: str, image_tools_dir: Path, overlay_dir: Path) -> ToolResult:
    """Re-validate `content`/`filename`, then write it into `overlay_dir` --
    never `image_tools_dir`. Refuses before ever touching disk when
    `filename` already names a file under `image_tools_dir`: an overlay
    write is never allowed to shadow a baseline image-bundle tool, or the
    fast lane would be a door for silently replacing an enforcement hook."""
    try:
        validate_filename(filename)
        validate_content(filename, content.encode("utf-8"))
    except ToolingError as exc:
        return ToolResult(stored=False, error=str(exc))

    if (Path(image_tools_dir) / filename).exists():
        return ToolResult(
            stored=False,
            error=f"a tool named {filename!r} already ships in the image bundle",
        )

    overlay = Path(overlay_dir)
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / filename).write_text(content, encoding="utf-8")
    return ToolResult(stored=True, filename=filename)
