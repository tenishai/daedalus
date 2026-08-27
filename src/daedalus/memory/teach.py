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

kobo's knowledge files live in bundle/knowledge/, which the boot seeder
(docker/seeder.py) ingests on its OWN filename-keyed ledger, seed_state,
under a `seed:{filename}` source -- a completely separate table from
intake_state. A file this module edits or deletes is very often one the
seeder already ingested at boot (a roster file refreshed over chat, for
instance), so apply_knowledge and apply_knowledge_delete read and maintain
seed_state too: retiring only intake_state would leave the boot-seeded
prior live forever, ghosting beside every edit and surviving every delete.

Both writers stamp seed_state with docker/seeder.py's staleness protocol:
an edit rewrites the row to the new content's sha and entry ids under
lane='live', with previous_sha chained back to the image-era sha; a delete
leaves a TOMBSTONE (lane='live', deleted=1, same previous_sha chaining)
instead of dropping the row. The runtime image these writes land in is
baked at build time and does not change until a later rebuild, so the
seeder that runs on EVERY boot needs a way to tell "this image predates the
fast-lane write" apart from "someone edited the file directly in the repo"
-- both look like a sha mismatch otherwise. previous_sha is that signal: a
stale image matches it and is skipped outright (no delete, no re-seed,
never reverts the edit or resurrects the delete); a rebuilt image matches
the new sha and converges the row back to plain lane='seed'; anything else
is genuinely new content and converges normally. See docker/seeder.py's
module docstring and sync() for the seeder's half of this protocol.

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

# The boot seeder's own ledger (docker/seeder.py), keyed by filename rather
# than sha, for bundle/knowledge/*.md files it ingested with a `seed:
# {filename}` source. apply_knowledge and apply_knowledge_delete read and
# maintain this table too -- not just intake_state -- because a filename
# the seeder already ingested at boot and a filename this module edits or
# deletes are frequently the SAME file (kobo's knowledge files live in
# bundle/knowledge/, seeded at boot, then edited/deleted live over this
# fast lane). Retiring only intake_state would leave the seeded prior
# live forever.
_SEED_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS seed_state (
       filename TEXT PRIMARY KEY,
       sha256 TEXT NOT NULL,
       entry_ids TEXT NOT NULL,
       lane TEXT NOT NULL DEFAULT 'seed',
       previous_sha TEXT,
       deleted INTEGER NOT NULL DEFAULT 0
   )"""


def _migrate_seed_state_schema(conn: sqlite3.Connection) -> None:
    """Additive ALTERs for a seed_state.db written before the staleness
    protocol's columns existed -- mirrors docker/seeder.py's own migration
    byte-for-byte (kept duplicated, not imported: this module and the boot
    seeder are parity-tested siblings, not a shared dependency). Safe to
    call every time; it is a no-op once the columns exist."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seed_state)")}
    if "lane" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN lane TEXT NOT NULL DEFAULT 'seed'")
    if "previous_sha" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN previous_sha TEXT")
    if "deleted" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    conn.commit()


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

    The same filename may ALSO have a seed_state row from the boot seeder
    (docker/seeder.py ingesting bundle/knowledge/{filename} at boot, source
    `seed:{filename}`) -- a second, independent ledger this function must
    also check and retire from, or the seeded prior stays live forever
    beside every edit. See the module docstring and _SEED_STATE_SCHEMA.

    That seed_state row is stamped lane='live' with previous_sha chained
    back to the image-era sha (the sha the row held before ANY unconverged
    live write, not just the immediately preceding one -- a second edit
    before a rebuild must not overwrite previous_sha with the first edit's
    sha, or the seeder loses the ability to recognize a still-stale image
    as stale). This is docker/seeder.py's staleness protocol: without it, a
    restart on an image that predates this write would hash the image's
    stale bytes, see them differ from this row's sha256, and "converge" by
    hard-deleting this write's entries and re-seeding the stale content --
    silently reverting the edit. Matching previous_sha instead tells the
    seeder to skip the file entirely on a stale boot.
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
        conn.execute(_SEED_STATE_SCHEMA)
        _migrate_seed_state_schema(conn)
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

        # The boot seeder may already have ingested this exact filename from
        # bundle/knowledge/ (source `seed:{filename}`, seed_state keyed by
        # filename) -- a separate ledger from intake_state's. An edit here
        # must retire THOSE entries too, or the seeded fact and the newly
        # taught one are both live at once (the ghost bug this closes).
        seeded = conn.execute(
            "SELECT sha256, entry_ids, lane, previous_sha FROM seed_state WHERE filename = ?",
            (filename,),
        ).fetchone()
        seeded_entry_ids: list[int] = json.loads(seeded[1]) if seeded else []
        if seeded is None:
            live_previous_sha = None
        elif seeded[2] == "live":
            # A live write (or tombstone) already sits here, unconverged --
            # its own sha256 is fast-lane content, not real image bytes, so
            # chain back to the previous_sha IT was carrying rather than
            # clobbering it with this intermediate sha. That previous_sha
            # is what a stale-image boot needs to compare against; losing
            # it here would make the seeder mistake a still-stale original
            # image for genuinely new content the very next boot.
            live_previous_sha = seeded[3]
        else:
            # An ordinary lane='seed' row: its sha256 IS the image-era sha
            # the running image was built with.
            live_previous_sha = seeded[0]

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

        retired_ids = prior_entry_ids + [
            entry_id for entry_id in seeded_entry_ids if entry_id not in prior_entry_ids
        ]
        if retired_ids:
            _retire_entries(db_path, retired_ids, ids[0])

        if prior_sha is not None:
            conn.execute("DELETE FROM intake_state WHERE sha256 = ?", (prior_sha,))
        conn.execute(
            "INSERT OR REPLACE INTO intake_state "
            "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
            (digest, filename, json.dumps(ids)),
        )
        if seeded is not None:
            # Rewrite (never drop) the seed_state row to describe THIS
            # write's content and live entries, stamped lane='live' with
            # previous_sha chained back to the image-era sha (see above).
            # The archive lane carries this exact text back into
            # bundle/knowledge/{filename} for the next image build, so the
            # next boot's seeder hashes identical bytes, matches this sha,
            # and calls the file unchanged (converging the row back to
            # plain seed lane) -- it never re-ingests under a fresh
            # seed:{filename} source and never double-accounts the entries
            # this write just stored. If instead that next boot's image is
            # STILL the stale one predating this write, the seeder matches
            # previous_sha and skips entirely, rather than reverting the
            # edit -- the whole point of tracking it.
            conn.execute(
                "INSERT OR REPLACE INTO seed_state "
                "(filename, sha256, entry_ids, lane, previous_sha, deleted) "
                "VALUES (?, ?, ?, 'live', ?, 0)",
                (filename, digest, json.dumps(ids), live_previous_sha),
            )
        conn.commit()
        return TeachResult(stored=True, entry_ids=ids, source=source)
    finally:
        conn.close()


def apply_knowledge_delete(filename: str, db_path: Path) -> TeachResult:
    """Delete `filename`'s knowledge: retire every entry EITHER ledger has on
    record for it and drop both ledger rows, so neither recall nor a future
    boot seeder can see it again.

    Two ledgers can hold entries for the same filename: intake_state (this
    module's own edit/delete lane, keyed by sha) and seed_state (the boot
    seeder's, docker/seeder.py, keyed by filename, source `seed:{filename}`).
    A file that was seeded at boot and never edited over this fast lane
    before has ONLY a seed_state row -- deleting it must still retire those
    entries, not just no-op because intake_state has nothing on record.

    Retirement never deletes a knowledge row (techne's own rule). A plain
    delete has no replacement content to serve as the live survivor
    `_retire_in_conn` requires, so a minimal marker entry is minted via
    `knowledge.supersede()` -- itself a mint-plus-retire in one call, using
    the file's own last entry (from whichever ledger has one) as the one it
    replaces -- to carry that role; the file's remaining entries, from
    BOTH ledgers, are then retired into that marker via _retire_entries.
    Only the marker stays live; every one of the file's real facts ends up
    superseded.

    If a seed_state row existed (the file was ever boot-seeded), this
    leaves a TOMBSTONE there instead of dropping it outright: lane='live',
    deleted=1, previous_sha chained back to the image-era sha the same way
    apply_knowledge does. Without a tombstone, a restart on an image that
    still has the pre-delete bytes (the archive lane's removal of
    bundle/knowledge/{filename} hasn't reached a rebuild yet) would find no
    ledger row for a file the image DOES have, treat it as brand new, and
    resurrect it. The tombstone lets docker/seeder.py's sync() recognize
    that exact image content as stale-and-already-deleted and skip it
    instead. Once a rebuild finally removes the file from the image too,
    sync() drops the tombstone on its own (see sync()'s removal loop).
    """
    if not _FILENAME_RE.match(filename or ""):
        return TeachResult(stored=False, error=f"invalid knowledge filename {filename!r}")

    from techne.core import knowledge

    state_db = Path(db_path).parent / "seed_state.db"
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_INTAKE_STATE_SCHEMA)
        conn.execute(_SEED_STATE_SCHEMA)
        _migrate_seed_state_schema(conn)
        conn.commit()
        intake_row = conn.execute(
            "SELECT sha256, entry_ids FROM intake_state WHERE filename = ?",
            (filename,),
        ).fetchone()
        seed_row = conn.execute(
            "SELECT sha256, entry_ids, lane, previous_sha FROM seed_state WHERE filename = ?",
            (filename,),
        ).fetchone()
        if intake_row is None and seed_row is None:
            return TeachResult(stored=False, error=f"no knowledge on record for {filename!r}")

        prior_sha = intake_row[0] if intake_row else None
        intake_entry_ids: list[int] = json.loads(intake_row[1]) if intake_row else []
        seed_entry_ids: list[int] = json.loads(seed_row[1]) if seed_row else []
        entry_ids = intake_entry_ids + [
            entry_id for entry_id in seed_entry_ids if entry_id not in intake_entry_ids
        ]

        if entry_ids:
            marker_source = f"intake:{filename}#deleted"
            survivor_id = knowledge.supersede(
                db_path,
                entry_ids[-1],
                f"[{filename} removed from knowledge]",
                marker_source,
            )
            _retire_entries(db_path, entry_ids[:-1], survivor_id)

        if prior_sha is not None:
            conn.execute("DELETE FROM intake_state WHERE sha256 = ?", (prior_sha,))
        if seed_row is not None:
            # Chain previous_sha back to the image-era sha exactly as
            # apply_knowledge does: a row already lane='live' (an edit that
            # never converged, or an earlier tombstone) carries the real
            # image-era sha in ITS previous_sha already; an ordinary
            # lane='seed' row's own sha256 IS the image-era sha.
            seed_sha, _entry_ids_raw, seed_lane, seed_previous_sha = seed_row
            tombstone_previous_sha = seed_previous_sha if seed_lane == "live" else seed_sha
            conn.execute(
                "INSERT OR REPLACE INTO seed_state "
                "(filename, sha256, entry_ids, lane, previous_sha, deleted) "
                "VALUES (?, ?, '[]', 'live', ?, 1)",
                (filename, tombstone_previous_sha, tombstone_previous_sha),
            )
        conn.commit()
        return TeachResult(
            stored=True, entry_ids=entry_ids, source=f"intake:{filename}#deleted"
        )
    finally:
        conn.close()
