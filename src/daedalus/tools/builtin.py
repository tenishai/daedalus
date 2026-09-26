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
        read_param = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path"},
                "offset": {
                    "type": "integer",
                    "description": "First line to read, 1-based. Give offset or limit "
                    "to read part of a file, with line numbers.",
                },
                "limit": {"type": "integer", "description": "Number of lines to read."},
            },
            "required": ["path"],
        }
        return [
            {
                "name": "files.read",
                "description": (
                    "Read a text file under the allowed roots. A file over "
                    f"{self.max_bytes} bytes must be read in parts with offset and limit."
                ),
                "parameters": read_param,
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
            if path.is_dir():
                raise IsADirectoryError(f"{raw!r} is a directory; use files.list to see it")
            if not path.is_file():
                raise FileNotFoundError(f"{raw!r} is not a file")
            offset = _line_arg(payload, "offset")
            limit = _line_arg(payload, "limit")
            if offset is None and limit is None:
                if path.stat().st_size > self.max_bytes:
                    lines = _count_lines(path)
                    raise ValueError(
                        f"{raw!r} exceeds the {self.max_bytes}-byte read limit; it has "
                        f"{lines} lines; read it in parts with offset and limit"
                    )
                return path.read_text(errors="replace")
            return self._read_range(raw, path, offset or 1, limit)

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

    def _read_range(self, raw: str, path: Path, offset: int, limit: int | None) -> str:
        """Lines offset..offset+limit-1, numbered. The byte cap applies to what is
        returned, so a small model can walk a big file a window at a time."""
        picked: list[str] = []
        total = 0
        with path.open(errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                total = number
                if number >= offset and (limit is None or number < offset + limit):
                    picked.append(f"{number}\t{line.rstrip(chr(10))}")
        if offset > total:
            raise ValueError(f"{raw!r} has {total} lines; offset {offset} is past the end")
        body = "\n".join(picked)
        size = len(body.encode())
        if size > self.max_bytes:
            fits, used = 0, 0
            for entry in picked:
                used += len(entry.encode()) + (1 if fits else 0)
                if used > self.max_bytes:
                    break
                fits += 1
            if fits == 0:
                raise ValueError(
                    f"line {offset} of {raw!r} alone exceeds the {self.max_bytes}-byte "
                    "read limit; it cannot be read with this tool"
                )
            raise ValueError(
                f"lines {offset}-{offset + len(picked) - 1} of {raw!r} exceed the "
                f"{self.max_bytes}-byte read limit; limit counts lines, and "
                f"{fits} lines fit from here"
            )
        last = offset + len(picked) - 1
        return f"[{raw} lines {offset}-{last} of {total}]\n{body}"


def _line_arg(payload: dict, key: str) -> int | None:
    """A positive line number or count. Models often send integers as strings."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key!r} must be a whole number, got {value!r}") from None
    if number < 1:
        raise ValueError(f"{key!r} must be 1 or more, got {number}")
    return number


def _count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)
