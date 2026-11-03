"""Per-task workspaces and metadata-bound tool arguments (#247).

A builtin root written as the literal `{workspace}` (or `{workspace}/sub`) is filled per
task, not at load: the task's metadata names one folder under the bundle's
`tools.workspace.parent`, and the files and exec tools are confined to it for that task.
The name is a single directory name, never a path, so a caller can choose which prepared
folder a task works in but can never widen the roots (the refusal in #199 stays closed
against input from the wire).

`tools.bind` fills some of a tool's arguments from the task's metadata on every call and
hides them from the model, so neither the model nor code it runs can choose them.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any

#: Task metadata key naming this task's workspace folder. A CROSS-REPO STRING CONTRACT:
#: callers (cabal's review workflow, agents' review-daedalus.sh) set it by this name.
WORKSPACE_KEY = "daedalus_workspace"

#: The root token. Literal braces, so `${VAR}` expansion at load leaves it alone.
TOKEN = "{workspace}"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: This task's resolved workspace, set by Agent._run_engine for the turn.
current_workspace: ContextVar[Path | None] = ContextVar("daedalus_workspace", default=None)

#: Providers kept per workspace, so a long task doesn't rebuild them on every call.
_CACHE_SIZE = 32


class WorkspaceError(ValueError):
    """A task's workspace name is missing, malformed, or doesn't resolve under the parent."""


def uses_token(roots) -> bool:
    return isinstance(roots, list) and any(
        isinstance(root, str) and (root == TOKEN or root.startswith(TOKEN + "/"))
        for root in roots
    )


def resolve(parent: Path, name) -> Path:
    """The task's workspace: `parent/name`, which must be an existing directory under
    `parent` after symlinks are resolved."""
    if not isinstance(name, str) or not name:
        raise WorkspaceError(f"this agent needs a workspace: set metadata {WORKSPACE_KEY!r}")
    if not _NAME.fullmatch(name):
        raise WorkspaceError(
            f"metadata {WORKSPACE_KEY!r} must be one directory name "
            f"(letters, digits, '.', '_', '-'), not {name!r}"
        )
    base = parent.resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path == base:
        raise WorkspaceError(f"workspace {name!r} resolves outside {base}")
    if not path.is_dir():
        raise WorkspaceError(f"workspace {name!r} is not a directory under {base}")
    return path


def expand(roots: list[str], workspace: Path) -> list[str]:
    """Roots with the token filled in. `{workspace}/sub` must stay under the workspace."""
    out: list[str] = []
    for root in roots:
        if root == TOKEN:
            out.append(str(workspace))
            continue
        if isinstance(root, str) and root.startswith(TOKEN + "/"):
            sub = PurePosixPath(root[len(TOKEN) + 1 :])
            if sub.is_absolute() or ".." in sub.parts or not sub.parts:
                raise WorkspaceError(f"root {root!r} must name a folder inside the workspace")
            path = (workspace / sub).resolve()
            if not path.is_relative_to(workspace):
                raise WorkspaceError(f"root {root!r} resolves outside the workspace")
            out.append(str(path))
            continue
        out.append(root)
    return out


def check_sub_roots(roots: list[str]) -> None:
    """Load-time check of `{workspace}/sub` roots, before any workspace exists."""
    for root in roots:
        if isinstance(root, str) and root.startswith(TOKEN + "/"):
            sub = PurePosixPath(root[len(TOKEN) + 1 :])
            if sub.is_absolute() or ".." in sub.parts or not sub.parts:
                raise ValueError(f"root {root!r} must name a folder inside the workspace")


class WorkspaceToolProvider:
    """A builtin group whose roots use the token: the real provider is built for the
    current task's workspace, and a call with no workspace fails closed."""

    def __init__(self, group, config: dict, parent: Path):
        roots = config.get("roots") or []
        check_sub_roots(roots)
        self.group = group
        self.config = config
        self.parent = parent
        self._built: OrderedDict[Path, Any] = OrderedDict()
        # Validate the rest of the config, and the parent against the group's own root
        # refusals, at load rather than on the first task.
        group(**{**config, "roots": [str(parent) if uses_token([r]) else r for r in roots]})

    def _provider(self):
        workspace = current_workspace.get()
        if workspace is None:
            raise WorkspaceError("no workspace is set for this task")
        provider = self._built.get(workspace)
        if provider is None:
            roots = expand(self.config.get("roots") or [], workspace)
            provider = self.group(**{**self.config, "roots": roots})
            self._built[workspace] = provider
            while len(self._built) > _CACHE_SIZE:
                self._built.popitem(last=False)
        else:
            self._built.move_to_end(workspace)
        return provider

    async def tools(self) -> list[dict]:
        return await self._provider().tools()

    async def call(self, name: str, payload: dict) -> str:
        return await self._provider().call(name, payload)


class BindError(ValueError):
    """A bound argument has no value in this task's metadata."""


def lookup(metadata: dict, path: str):
    """`metadata` at a dotted path, or BindError when any step is missing."""
    node = metadata
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise BindError(f"task metadata has no {path!r}")
        node = node[part]
    if node is None or node == "":
        raise BindError(f"task metadata {path!r} is empty")
    return node


def bind_arguments(arguments: dict, binding: dict[str, str], metadata: dict) -> dict:
    """The call's arguments with every bound one overwritten from metadata."""
    return {**arguments, **{arg: lookup(metadata, path) for arg, path in binding.items()}}


def hide_bound(spec: dict, binding: dict[str, str]) -> dict:
    """The tool spec as the model sees it: bound arguments removed."""
    params = spec.get("parameters")
    if not isinstance(params, dict):
        return spec
    props = {k: v for k, v in (params.get("properties") or {}).items() if k not in binding}
    required = [k for k in params.get("required") or [] if k not in binding]
    hidden = {**params, "properties": props}
    if "required" in params:
        hidden["required"] = required
    return {**spec, "parameters": hidden}
