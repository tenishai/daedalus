"""The teach system task: an operator's note becomes a techne memory NOW.

kobo relays a metadata-marked a2a task through the switchboard; the agent
intercepts it pre-model (daedalus.agent.Agent.handle_task) and calls
apply_teach here. The write mirrors the boot seeder (docker/seeder.py)
byte-for-byte in provenance and outcome: same sectioning, same type
mapping, same store call, same intake_state ledger row -- so the next
natural rebuild's seeder sees the sha already ledgered and skips the
archived file. Parity is enforced by tests/test_memory_teach.py, which
runs the real seeder against the same bytes; change one side and the
suite says so.

Authorization gates on the transport-stamped verified JWT subject
(daedalus.task.VERIFIED_SUB_KEY): colleagues dispatch as "agent:<name>"
and must never mint operator-grade intake:chat-* facts (the operator
shield's provenance would be forged). No verified sub means refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from daedalus.task import VERIFIED_SUB_KEY, Task

SYSTEM_TASK_KEY = "system_task"
TEACH_MARKER = "teach"
FILENAME_KEY = "filename"

# The intake filename alphabet, mirroring the room's own intake rule:
# lowercase [a-z0-9-], at most 40 chars before ".md", no leading dash.
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\.md$")

# Sectioning and typing mirror docker/seeder.py exactly (parity-tested).
_H2 = re.compile(r"^## ", re.MULTILINE)
_TYPE_MAP = {
    "gotchas": "evaluation",
    "playbooks": "procedure",
    "operational-playbooks": "procedure",
    "decisions": "decision",
}

_INTAKE_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS intake_state (
       sha256 TEXT PRIMARY KEY,
       filename TEXT NOT NULL,
       entry_ids TEXT NOT NULL
   )"""


@dataclass
class TeachResult:
    stored: bool
    already_known: bool = False
    error: str | None = None
    entry_ids: list[int] = field(default_factory=list)
    source: str = ""


def is_teach_task(task: Task) -> bool:
    return task.metadata.get(SYSTEM_TASK_KEY) == TEACH_MARKER


def teach_authorized(task: Task) -> bool:
    sub = task.metadata.get(VERIFIED_SUB_KEY)
    return isinstance(sub, str) and bool(sub) and not sub.startswith("agent:")


def _sections(text: str) -> list[str]:
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        whole = text.strip()
        return [whole] if whole else []
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if section.removeprefix("##").strip():
            sections.append(section)
    return sections


def apply_teach(note: str, filename: str, db_path: Path) -> TeachResult:
    """Store `note` into the techne db at `db_path` with seeder-identical
    provenance, and ledger its sha in the seeder's own intake_state table
    (beside the techne db, `<dir>/seed_state.db`). Synchronous sqlite;
    callers on the event loop run it via asyncio.to_thread."""
    if not _FILENAME_RE.match(filename or ""):
        return TeachResult(stored=False, error=f"invalid teach filename {filename!r}")
    sections = _sections(note)
    if not sections:
        return TeachResult(stored=False, error="the note is empty")

    from techne.core import knowledge

    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    source = f"intake:{filename}#{digest[:12]}"
    kind = _TYPE_MAP.get(Path(filename).stem, "fact")
    state_db = Path(db_path).parent / "seed_state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_INTAKE_STATE_SCHEMA)
        conn.commit()
        row = conn.execute(
            "SELECT 1 FROM intake_state WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row is not None:
            return TeachResult(stored=False, already_known=True, source=source)

        # Crash recovery, same as the seeder: rows with this exact source
        # from an interrupted earlier attempt are replaced, never doubled.
        for entry in knowledge.list_entries(
            db_path, limit=10_000_000, include_superseded=True
        ):
            if entry.get("source") == source:
                knowledge.delete(db_path, entry["id"])

        ids: list[int] = []
        try:
            for section in sections:
                ids.append(
                    knowledge.store(
                        db_path,
                        section,
                        type=kind,
                        tags="intake",
                        confidence="high",
                        source=source,
                    )
                )
        except Exception:
            for entry_id in ids:
                try:
                    knowledge.delete(db_path, entry_id)
                except Exception:
                    pass
            raise
        conn.execute(
            "INSERT OR REPLACE INTO intake_state "
            "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
            (digest, filename, json.dumps(ids)),
        )
        conn.commit()
        return TeachResult(stored=True, entry_ids=ids, source=source)
    finally:
        conn.close()
