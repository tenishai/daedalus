"""The system-task handler family: an operator's write becomes a techne
memory NOW, pre-model.

kobo relays a metadata-marked a2a task through the switchboard; the agent
intercepts it pre-model (daedalus.agent.Agent.handle_task) and dispatches on
task.metadata["system_task"] to one of the handlers here rather than ever
reaching the model. Three markers exist:

  - TEACH_MARKER ("teach"): an append-only operator note. apply_teach writes
    it and is untouched by this module's knowledge additions.
  - KNOWLEDGE_MARKER ("knowledge"): a FILE with edit/delete semantics, not a
    note. apply_knowledge and apply_knowledge_delete below.
  - TOOL_MARKER ("tool"): a fast-delivered tool file, re-validated and
    written into a persistent overlay directory on the agent's own volume.
    apply_tool lives in daedalus.memory.tooling (a sibling module, not
    here: the mechanical validation it ports from kobo_api.tooling has
    nothing in common with this module's techne-store plumbing), and its
    dispatch is Agent._handle_tool, not a function in this file.

Both apply_teach and apply_knowledge mirror the boot seeder (docker/seeder.py)
byte-for-byte in provenance and outcome for a file's first write: same
sectioning, same type mapping, same store call, same intake_state ledger row
-- so the next natural rebuild's seeder sees the sha already ledgered and
skips the archived file. Parity is enforced by tests/test_memory_teach.py,
which runs the real seeder against the same bytes; change one side and the
suite says so.

Knowledge's edit/delete semantics build on top of that shared store path:
the intake_state ledger already maps filename -> the entry_ids live for it,
so it doubles as the replacement machinery. An edit retires the entries
recorded for the filename's earlier sha before the new content lands under
the new sha; a delete retires them and drops the ledger row outright.
"Retire" here is techne's own belief-revision primitive (techne.core.
knowledge._retire_in_conn, techne#213) -- the same one the revision pass
uses to resolve a contradiction between two live facts -- not a hard
delete: techne never removes a knowledge row, it only ever flips
`superseded_by`, which is the one gate every recall path (FTS, vector,
list_entries, egress) honors unconditionally. See _retire_entries below for
why this, and not the softer `status='archived'` lifecycle flag, is the
primitive that actually removes an entry from recall.

Authorization gates on the transport-stamped verified JWT subject
(daedalus.task.VERIFIED_SUB_KEY): colleagues dispatch as "agent:<name>"
and must never mint operator-grade intake:chat-* facts (the operator
shield's provenance would be forged). No verified sub means refused. Every
marker's write carries exactly this same requirement -- teach_authorized is
reused verbatim, not reimplemented per marker.
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
KNOWLEDGE_MARKER = "knowledge"
TOOL_MARKER = "tool"
FILENAME_KEY = "filename"
# Knowledge-only: set (truthy) in metadata to route a KNOWLEDGE_MARKER task
# to apply_knowledge_delete instead of apply_knowledge. Absent/falsy is a
# write. kobo's DELETE route (task-3) stamps this alongside FILENAME_KEY.
DELETE_KEY = "delete"

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


def _retire_entries(db_path: Path, entry_ids: list[int], survivor_id: int) -> None:
    """Retire each of `entry_ids` via techne's own belief-revision retire
    primitive (`techne.core.knowledge._retire_in_conn`, techne#213) -- the
    same mechanism the revision pass uses to resolve a contradiction between
    two live facts. Retiring points `superseded_by` at `survivor_id` and
    never deletes the row: techne's own rule is that a knowledge row is
    never hard-deleted, only ever marked superseded.

    `superseded_by IS NULL` is the one gate every recall path in techne
    honors unconditionally -- FTS search, vector search, list_entries,
    egress's Tier-0 injection -- so this is what actually removes an entry
    from recall. The softer `status='archived'` lifecycle flag (used by
    techne's archive_scan for stale/uncertain facts) is NOT equivalent here:
    explicit search defaults `include_archived=True`, and
    daedalus.memory.techne.TechneMemoryStore.recall() calls search() with
    that same default -- an archived-only row would still surface through
    the agent's actual recall path. Only superseded_by is unconditional.

    `_retire_in_conn` requires `survivor_id` to already be a LIVE row (it is
    designed for contradiction detection, where both sides already exist as
    independent facts) -- narrower than a plain filename replace/delete,
    which has no natural paired survivor. apply_knowledge and
    apply_knowledge_delete each arrange for a real live survivor before
    calling this (the freshly stored new content, or a minted marker entry
    when there is none), so every retirement here is a normal, honest use
    of the primitive -- never a fabricated pairing.
    """
    if not entry_ids:
        return

    from techne.core.database import connect
    from techne.core.knowledge import _retire_in_conn

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for entry_id in entry_ids:
            if entry_id == survivor_id:
                continue
            row = conn.execute(
                "SELECT superseded_by FROM knowledge WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None or row["superseded_by"] is not None:
                continue  # already gone, or already retired -- idempotent
            _retire_in_conn(conn, entry_id, survivor_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_knowledge(text: str, filename: str, db_path: Path) -> TeachResult:
    """Store `text` as `filename`'s CURRENT knowledge -- an edit/replace, not
    an append. `filename` is the replacement key: the intake_state ledger
    already maps it to whatever entry_ids are live for it now.

    The new content is stored via the exact same store call, provenance, and
    ledger stamp apply_teach uses (same sectioning, same type mapping) --
    reused, not copied, so a file's FIRST write through this door produces
    byte-identical rows to what the boot seeder or apply_teach would have
    produced for the same bytes (parity-tested).

    What makes this an edit and not a second teach: before the new sha is
    stamped over the ledger row, whatever entries the ledger recorded for
    this filename's PRIOR sha are retired (see _retire_entries) into the
    first freshly-stored entry as survivor. The stale version can then never
    be recalled again, and the prior sha's now-superseded ledger row is
    dropped so `filename` never resolves to two ledger rows at once.
    """
    if not _FILENAME_RE.match(filename or ""):
        return TeachResult(stored=False, error=f"invalid knowledge filename {filename!r}")
    sections = _sections(text)
    if not sections:
        return TeachResult(stored=False, error="the note is empty")

    from techne.core import knowledge

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
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

        prior = conn.execute(
            "SELECT sha256, entry_ids FROM intake_state WHERE filename = ?",
            (filename,),
        ).fetchone()
        prior_sha = prior[0] if prior else None
        prior_entry_ids: list[int] = json.loads(prior[1]) if prior else []

        # Crash recovery, same as apply_teach: rows with this exact source
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

        if prior_entry_ids:
            _retire_entries(db_path, prior_entry_ids, ids[0])

        if prior_sha is not None:
            conn.execute("DELETE FROM intake_state WHERE sha256 = ?", (prior_sha,))
        conn.execute(
            "INSERT OR REPLACE INTO intake_state "
            "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
            (digest, filename, json.dumps(ids)),
        )
        conn.commit()
        return TeachResult(stored=True, entry_ids=ids, source=source)
    finally:
        conn.close()


def apply_knowledge_delete(filename: str, db_path: Path) -> TeachResult:
    """Delete `filename`'s knowledge: retire every entry the ledger has on
    record for it and drop the ledger row, so neither recall nor a future
    boot seeder can see it again.

    Retirement never deletes a knowledge row (techne's own rule). A plain
    delete has no replacement content to serve as the live survivor
    `_retire_in_conn` requires, so a minimal marker entry is minted via
    `knowledge.supersede()` -- itself a mint-plus-retire in one call, using
    the file's own last entry as the one it replaces -- to carry that role;
    the file's remaining entries are then retired into that marker via
    _retire_entries. Only the marker stays live; every one of the file's
    real facts ends up superseded.
    """
    if not _FILENAME_RE.match(filename or ""):
        return TeachResult(stored=False, error=f"invalid knowledge filename {filename!r}")

    from techne.core import knowledge

    state_db = Path(db_path).parent / "seed_state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_INTAKE_STATE_SCHEMA)
        conn.commit()
        row = conn.execute(
            "SELECT sha256, entry_ids FROM intake_state WHERE filename = ?",
            (filename,),
        ).fetchone()
        if row is None:
            return TeachResult(stored=False, error=f"no knowledge on record for {filename!r}")
        prior_sha, entry_ids_json = row
        entry_ids: list[int] = json.loads(entry_ids_json)

        if entry_ids:
            marker_source = f"intake:{filename}#deleted"
            survivor_id = knowledge.supersede(
                db_path,
                entry_ids[-1],
                f"[{filename} removed from knowledge]",
                marker_source,
            )
            _retire_entries(db_path, entry_ids[:-1], survivor_id)

        conn.execute("DELETE FROM intake_state WHERE sha256 = ?", (prior_sha,))
        conn.commit()
        return TeachResult(
            stored=True, entry_ids=entry_ids, source=f"intake:{filename}#deleted"
        )
    finally:
        conn.close()
