"""Built-in read-only files tools — rooted, fail-closed, no dependencies.

The out-of-box answer to "read a file" without node or third-party servers.
Write access never lives here; that's gateway (liminal) territory.
"""

from __future__ import annotations

from pathlib import Path

_LIST_CAP = 500


class FilesToolProvider:
    """files.read / files.list, confined to configured roots.

    Paths are resolved (symlinks included) and must land under a root —
    traversal and symlink escapes fail before any I/O.
    """

    def __init__(self, roots: list[str], max_bytes: int = 200_000):
        if not roots:
            raise ValueError("files builtin requires at least one root")
        self.roots = [Path(root).resolve() for root in roots]
        self.max_bytes = max_bytes

    async def tools(self) -> list[dict]:
        path_param = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path"}},
            "required": ["path"],
        }
        return [
            {
                "name": "files.read",
                "description": "Read a text file under the allowed roots.",
                "parameters": path_param,
            },
            {
                "name": "files.list",
                "description": "List a directory under the allowed roots.",
                "parameters": path_param,
            },
        ]

    def _resolve(self, raw: str) -> Path:
        resolved = Path(raw).resolve()
        if not any(resolved.is_relative_to(root) for root in self.roots):
            allowed = ", ".join(str(root) for root in self.roots)
            raise PermissionError(f"{raw!r} is outside the allowed roots ({allowed})")
        return resolved

    async def call(self, name: str, payload: dict) -> str:
        raw = payload.get("path")
        if not raw:
            raise ValueError("'path' is required")
        path = self._resolve(raw)

        if name == "files.read":
            if not path.is_file():
                raise FileNotFoundError(f"{raw!r} is not a file")
            if path.stat().st_size > self.max_bytes:
                raise ValueError(f"{raw!r} exceeds the {self.max_bytes}-byte read limit")
            return path.read_text(errors="replace")

        if name == "files.list":
            if not path.is_dir():
                raise NotADirectoryError(f"{raw!r} is not a directory")
            entries = sorted(
                entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir()
            )
            clipped = entries[:_LIST_CAP]
            if len(entries) > _LIST_CAP:
                clipped.append(f"... ({len(entries) - _LIST_CAP} more)")
            return "\n".join(clipped) if clipped else "(empty directory)"

        raise LookupError(f"unknown tool {name!r}")
