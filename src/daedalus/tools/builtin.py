"""Built-in read-only files tools — rooted, fail-closed, no dependencies.

The out-of-box answer to "read a file" without node or third-party servers.
Write access never lives here; that's gateway (liminal) territory.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

_LIST_CAP = 500
# files.search bounds. Each one is stated in the output when it bites, never silent.
_SEARCH_DEFAULT = 50
_SEARCH_CAP = 200
_SEARCH_FILE_BYTES = 2_000_000  # larger files are skipped, and the skip is counted
_SEARCH_LINE_CHARS = 300
_BINARY_SNIFF = 8192
_SEARCH_SKIP_DIRS = {"node_modules", "__pycache__"}


class FilesToolProvider:
    """files.read / files.list / files.search, confined to configured roots.

    Paths are resolved (symlinks included) and must land under a root —
    traversal and symlink escapes fail before any I/O.
    """

    def __init__(self, roots: list[str], max_bytes: int | str = 200_000):
        if not roots:
            raise ValueError("files builtin requires at least one root")
        self.roots = [Path(root).resolve() for root in roots]
        # ${VAR:-default} in agent.yaml always yields a string, and this config isn't
        # schema-typed, so take "40000" as 40000 (or refuse it at load).
        try:
            self.max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            raise ValueError(f"max_bytes must be a whole number, not {max_bytes!r}") from None
        if self.max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, not {self.max_bytes}")

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
            {
                "name": "files.search",
                "description": (
                    "Search file contents under the allowed roots for a regular "
                    "expression. Returns path:line: text for each match; read around a "
                    "match with files.read offset and limit. Hidden files and "
                    "directories are skipped unless path points into one."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regular expression, or plain text with fixed.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Absolute directory or file to search. "
                            "Default: every allowed root.",
                        },
                        "glob": {
                            "type": "string",
                            "description": "Only files whose name matches, e.g. *.py",
                        },
                        "fixed": {
                            "type": "boolean",
                            "description": "Treat pattern as plain text.",
                        },
                        "ignore_case": {"type": "boolean"},
                        "max_results": {
                            "type": "integer",
                            "description": f"Default {_SEARCH_DEFAULT}, at most {_SEARCH_CAP}.",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        ]

    def _resolve(self, raw: str) -> Path:
        resolved = Path(raw).resolve()
        if not any(resolved.is_relative_to(root) for root in self.roots):
            allowed = ", ".join(str(root) for root in self.roots)
            raise PermissionError(f"{raw!r} is outside the allowed roots ({allowed})")
        return resolved

    async def call(self, name: str, payload: dict) -> str:
        if name == "files.search":
            return self._search(payload)
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

    def _search(self, payload: dict) -> str:
        """Matches as absolute path:line: text, bounded and confined to the roots.

        Walks without following symlinked directories, and re-checks every file
        against the roots, so a symlink cannot lead the search outside them."""
        pattern = payload.get("pattern")
        if not pattern:
            raise ValueError("'pattern' is required")
        flags = re.IGNORECASE if _truthy(payload.get("ignore_case")) else 0
        text = re.escape(pattern) if _truthy(payload.get("fixed")) else pattern
        try:
            regex = re.compile(text, flags)
        except re.error as exc:
            raise ValueError(
                f"{pattern!r} is not a valid regular expression ({exc}); escape special "
                "characters, or pass fixed: true to search for it as plain text"
            ) from None
        limit = _line_arg(payload, "max_results") or _SEARCH_DEFAULT
        limit = min(limit, _SEARCH_CAP)
        glob = payload.get("glob") or None
        starts = [self._resolve(payload["path"])] if payload.get("path") else self.roots
        for start in starts:
            if not start.exists():
                raise FileNotFoundError(f"{str(start)!r} does not exist")

        hits: list[str] = []
        skipped_big = 0
        stopped = False
        for path in self._walk(starts, glob):
            try:
                if path.stat().st_size > _SEARCH_FILE_BYTES:
                    skipped_big += 1
                    continue
                with path.open("rb") as sniff:
                    if b"\0" in sniff.read(_BINARY_SNIFF):
                        continue
                with path.open(errors="replace") as handle:
                    for number, line in enumerate(handle, start=1):
                        if regex.search(line):
                            shown = line.rstrip("\n")
                            if len(shown) > _SEARCH_LINE_CHARS:
                                shown = shown[:_SEARCH_LINE_CHARS] + " …"
                            if len(hits) == limit:  # one past the limit: there is more
                                stopped = True
                                break
                            hits.append(f"{path}:{number}: {shown}")
            except OSError:
                continue
            if stopped:
                break

        where = ", ".join(str(start) for start in starts)
        notes = []
        if stopped:
            notes.append(
                f"(showing the first {_count(limit, 'match', 'matches')}; there are more; "
                "narrow the pattern, path or glob to see them)"
            )
        if skipped_big:
            notes.append(
                f"({skipped_big} file(s) over {_SEARCH_FILE_BYTES} bytes were not searched)"
            )
        if not hits:
            return "\n".join([f"no matches for {pattern!r} in {where}", *notes])
        return "\n".join([*hits, *notes])

    def _walk(self, starts: list[Path], glob: str | None):
        for start in starts:
            if start.is_file():
                candidates = [start]
            else:
                # Hidden names are skipped, unless the search starts inside a hidden
                # directory (e.g. .github): then everything below it is covered.
                # Dependency directories are skipped either way.
                skip_hidden = not self._inside_hidden(start)
                candidates = []
                for folder, dirs, files in os.walk(start):  # followlinks=False
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if d not in _SEARCH_SKIP_DIRS and not (skip_hidden and d.startswith("."))
                    )
                    candidates.extend(
                        Path(folder) / name
                        for name in sorted(files)
                        if not (skip_hidden and name.startswith("."))
                    )
            for path in candidates:
                if glob and not fnmatch.fnmatch(path.name, glob):
                    continue
                resolved = path.resolve()
                if path.is_symlink() and not any(
                    resolved.is_relative_to(root) for root in self.roots
                ):
                    continue
                if resolved.is_file():
                    yield path

    def _inside_hidden(self, start: Path) -> bool:
        """True when start is, or is under, a hidden directory below any root that
        contains it, so the answer does not depend on the order roots are listed."""
        return any(
            any(part.startswith(".") for part in start.relative_to(root).parts)
            for root in self.roots
            if start.is_relative_to(root)
        )

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
                if line is None:  # a line longer than the cap: over it by definition
                    entry, size = "", self.max_bytes + 1
                else:
                    entry = f"{number}\t{line}"
                    size = len(entry.encode()) + (1 if picked else 0)
                if used + size > self.max_bytes:
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


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _truthy(value) -> bool:
    """Booleans from models arrive as true, "true" or "1"."""
    return value is True or str(value).lower() in ("true", "1", "yes")


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
