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
                        f"{_lines(lines)}; read it in parts with offset and limit"
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
        """Lines offset..offset+limit-1, numbered. The byte cap applies to the lines
        returned (the one-line header is not counted), so a small model can walk a big
        file a window at a time.

        Memory stays bounded by the cap, not the file (PR #94 review): the line count
        is taken in binary chunks, lines are read with a size bound, and reading stops
        as soon as the window is full or over the cap. Time is not bounded: counting
        is one pass over the whole file, and reaching offset reads every line before
        it."""
        total = _count_lines(path)
        if total == 0:
            return f"[{raw} is empty: 0 lines]"
        if offset > total:
            raise ValueError(
                f"{raw!r} has {_lines(total)}; offset {offset} is past the end"
            )
        last = total if limit is None else min(total, offset + limit - 1)
        picked: list[str] = []
        used = 0
        with path.open(errors="replace") as handle:
            for number, line in enumerate(_bounded_lines(handle, self.max_bytes), start=1):
                if number < offset:
                    continue
                if number > last:
                    break
                entry = None if line is None else f"{number}\t{line}"
                size = None if entry is None else len(entry.encode()) + (1 if picked else 0)
                if size is None or used + size > self.max_bytes:
                    if not picked:
                        raise ValueError(
                            f"line {offset} of {raw!r} alone exceeds the "
                            f"{self.max_bytes}-byte read limit; it cannot be read "
                            "with this tool"
                        )
                    raise ValueError(
                        f"lines {offset}-{last} of {raw!r} exceed the "
                        f"{self.max_bytes}-byte read limit; limit counts lines, and "
                        f"{_lines(len(picked))} fit from here"
                    )
                picked.append(entry)
                used += size
        body = "\n".join(picked)
        return f"[{raw} lines {offset}-{last} of {total}]\n{body}"


def _bounded_lines(handle, cap: int):
    """Each line without its newline, or None for a line longer than cap characters.
    No line is ever held in full beyond cap + 1 characters."""
    while True:
        piece = handle.readline(cap + 1)
        if not piece:
            return
        if piece.endswith("\n") or len(piece) <= cap:
            yield piece.rstrip("\n")
            continue
        while piece and not piece.endswith("\n"):  # skip the rest of an over-long line
            piece = handle.readline(cap + 1)
        yield None


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


_CHUNK = 1 << 20


def _count_lines(path: Path) -> int:
    """Lines as _read_range numbers them (universal newlines: \\n, \\r\\n and a bare
    \\r each end one), counted in binary chunks so a huge or newline-free file costs
    one pass of I/O and no more memory than a chunk."""
    count = 0
    prev_cr = False
    last = b""
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            count += chunk.count(b"\n") + chunk.count(b"\r") - chunk.count(b"\r\n")
            if prev_cr and chunk.startswith(b"\n"):
                count -= 1  # a \r\n split across chunks was counted twice
            prev_cr = chunk.endswith(b"\r")
            last = chunk[-1:]
    if last and last not in (b"\n", b"\r"):
        count += 1  # a final line with no newline
    return count


def _lines(count: int) -> str:
    return f"{count} line" if count == 1 else f"{count} lines"
