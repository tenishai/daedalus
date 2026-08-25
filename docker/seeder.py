"""Convergent knowledge seeder for the daedalus runtime image.

Seeds bundle/knowledge/*.md into a techne SQLite store at boot. Convergent
per file: only files whose content hash changed are re-seeded; entries from
removed files are dropped; learned (non-seed) entries are never touched.

Section convention: knowledge files are split on `## ` headings; the filename
stem picks the techne knowledge type (see TYPE_MAP; anything else is a fact).

bundle/intake/*.{md,txt} gets the opposite lifecycle: a one-shot append pass
(see ingest_intake) ledgered by content-sha rather than filename. Once a
file's sha is ledgered it is never touched again — not on content change
(new sha, new ingestion), not on deletion (memories outlive the file). Never
part of knowledge convergence: intake rows never appear in seed_state and
are never removed or counted by sync().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

TYPE_MAP = {
    "gotchas": "evaluation",
    "playbooks": "procedure",
    "operational-playbooks": "procedure",
    "decisions": "decision",
}

_H2 = re.compile(r"^## ", re.MULTILINE)


def type_for(filename: str) -> str:
    return TYPE_MAP.get(Path(filename).stem, "fact")


def parse_knowledge_file(path: Path) -> list[str]:
    text = path.read_text()
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        # No `## ` headings at all: intake notes are frequently flat prose
        # rather than knowledge-file-style sections, so the whole (trimmed)
        # file becomes its one section instead of yielding nothing.
        whole = text.strip()
        return [whole] if whole else []
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if section.removeprefix("##").strip():
            sections.append(section)
    return sections


log = logging.getLogger("seeder")

_SEED_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS seed_state (
       filename TEXT PRIMARY KEY,
       sha256 TEXT NOT NULL,
       entry_ids TEXT NOT NULL
   )"""

_INTAKE_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS intake_state (
       sha256 TEXT PRIMARY KEY,
       filename TEXT NOT NULL,
       entry_ids TEXT NOT NULL
   )"""


def _state_conn(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_SEED_STATE_SCHEMA)
        conn.execute(_INTAKE_STATE_SCHEMA)
    except sqlite3.DatabaseError:
        # Corrupt file (e.g. an unclean kill of a persistent volume left a
        # malformed sqlite image) — close and let the caller quarantine it.
        conn.close()
        raise
    return conn


def _load_state(state_db: Path) -> tuple[sqlite3.Connection, dict]:
    """Open the state db and read its rows.

    Raises sqlite3.DatabaseError if the on-disk file is corrupt; the caller
    is responsible for quarantining and rebuilding in that case.
    """
    conn = _state_conn(state_db)
    try:
        state = {
            row[0]: {"sha256": row[1], "entry_ids": json.loads(row[2])}
            for row in conn.execute("SELECT filename, sha256, entry_ids FROM seed_state")
        }
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn, state


def sync(
    knowledge_dir: Path,
    techne_db: Path,
    state_db: Path,
    store_fn,
    delete_fn,
    purge_fn=None,
    list_ids_fn: Callable[[Path, str], list[int]] | None = None,
) -> dict:
    try:
        conn, state = _load_state(state_db)
    except sqlite3.DatabaseError:
        log.warning("state db %s is corrupt — quarantining and rebuilding", state_db)
        if purge_fn is not None:
            # Rebuilding from empty state would re-seed everything as new
            # entries, duplicating whatever was already stored under the
            # lost state — purge the prior seed-sourced entries first.
            # Run before the quarantine rename: purge is idempotent, so if
            # it raises mid-way, recovery is retryable on the next run
            # (the corrupt state db is still in place to retry against)
            # instead of leaving duplicated entries with no way back in.
            purge_fn(techne_db)
        os.replace(state_db, str(state_db) + ".corrupt")
        conn, state = _load_state(state_db)

    try:
        current = {p.name: p for p in sorted(knowledge_dir.glob("*.md"))}
        seeded = removed = unchanged = 0

        for filename in set(state) - set(current):
            for entry_id in state[filename]["entry_ids"]:
                delete_fn(techne_db, entry_id)
            conn.execute("DELETE FROM seed_state WHERE filename = ?", (filename,))
            conn.commit()
            removed += 1

        for filename, path in current.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            prior = state.get(filename)
            if prior and prior["sha256"] == digest:
                unchanged += 1
                continue
            if prior:
                for entry_id in prior["entry_ids"]:
                    delete_fn(techne_db, entry_id)
            elif list_ids_fn is not None:
                # Ground-truth convergence: no state row for this filename
                # means either it's genuinely new, or a SIGKILL previously
                # landed between a store_fn() call and the state-row commit
                # below — no exception was raised, so the except-block
                # cleanup never ran, and no state row was written to
                # remember the entries either. Either way techne itself is
                # the ground truth, so drop anything already sourced from
                # this exact file before storing fresh entries — a restart
                # can then never leave duplicates behind.
                for entry_id in list_ids_fn(techne_db, f"seed:{filename}"):
                    delete_fn(techne_db, entry_id)

            ids: list[int] = []
            try:
                for section in parse_knowledge_file(path):
                    ids.append(
                        store_fn(techne_db, section, type_for(filename), "seed", f"seed:{filename}")
                    )
            except Exception:
                # Best-effort cleanup so a mid-file store failure never
                # orphans entries the state db has no record of.
                for entry_id in ids:
                    try:
                        delete_fn(techne_db, entry_id)
                    except Exception:
                        log.warning(
                            "cleanup failed for orphaned entry %s from %s", entry_id, filename
                        )
                raise

            conn.execute(
                "INSERT OR REPLACE INTO seed_state (filename, sha256, entry_ids) VALUES (?, ?, ?)",
                (filename, digest, json.dumps(ids)),
            )
            conn.commit()
            seeded += len(ids)

        return {"seeded": seeded, "removed": removed, "unchanged": unchanged}
    finally:
        conn.close()


def ingest_intake(
    intake_dir: Path,
    techne_db: Path,
    state_db: Path,
    store_fn,
    delete_fn,
    list_ids_fn: Callable[[Path, str], list[int]] | None = None,
) -> dict:
    """One-shot append pass over bundle/intake/*.{md,txt}.

    Opposite lifecycle from sync(): ledgered by content-sha (not filename),
    so filename reuse with new content is a new ingestion, and a file's
    disappearance from disk removes nothing — intake files are a disposable
    delivery vehicle, the memories they produce are not. There is never a
    removal pass here; a missing intake_dir simply yields no files (same
    Path.glob-on-nonexistent-dir behavior sync() already relies on).

    Each newly-ledgered file is stored under a source scoped to its own
    content-sha (`intake:<filename>#<sha12>`), not just its filename. That
    scoping is what lets a crash between storing entries and writing the
    ledger row converge without duplicates: on the next boot, list_ids_fn
    (when provided) is asked for that exact sha-scoped source, so it can
    only ever find that same orphaned run's rows — never a legitimately
    reused filename's entries, which get their own sha and their own source.
    """
    conn = _state_conn(state_db)
    try:
        ledgered = {row[0] for row in conn.execute("SELECT sha256 FROM intake_state")}
        files = sorted(intake_dir.glob("*.md")) + sorted(intake_dir.glob("*.txt"))
        ingested = skipped = 0

        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in ledgered:
                skipped += 1
                continue

            source = f"intake:{path.name}#{digest[:12]}"
            if list_ids_fn is not None:
                for entry_id in list_ids_fn(techne_db, source):
                    delete_fn(techne_db, entry_id)

            ids: list[int] = []
            try:
                for section in parse_knowledge_file(path):
                    ids.append(
                        store_fn(techne_db, section, type_for(path.name), "intake", source)
                    )
            except Exception:
                # Best-effort cleanup so a mid-file store failure never
                # orphans entries the state db has no record of.
                for entry_id in ids:
                    try:
                        delete_fn(techne_db, entry_id)
                    except Exception:
                        log.warning(
                            "cleanup failed for orphaned entry %s from %s", entry_id, path.name
                        )
                raise

            conn.execute(
                "INSERT OR REPLACE INTO intake_state "
                "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
                (digest, path.name, json.dumps(ids)),
            )
            conn.commit()
            ledgered.add(digest)
            ingested += len(ids)

        return {"ingested": ingested, "skipped": skipped}
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="seeder: %(message)s")
    if len(argv) != 2:
        log.error("usage: seeder.py <bundle_dir>")
        return 2
    bundle = Path(argv[1])
    knowledge_dir = bundle / "knowledge"
    # knowledge_dir may legitimately not exist: git drops empty directories,
    # so deleting the last knowledge file from a bundle removes knowledge/
    # from the built image entirely, not merely empties it. sync() already
    # treats a missing knowledge_dir as zero current files (Path.glob on a
    # nonexistent dir yields nothing, same as an empty one), so it must
    # still run here — otherwise state-tracked removals never purge and
    # the deleted file's seeded rows persist in techne forever.
    home = Path(os.environ.get("TECHNE_HOME", "/techne"))
    home.mkdir(parents=True, exist_ok=True)
    techne_db = home / "techne.db"

    from techne.core import knowledge
    from techne.core.database import init_db

    init_db(techne_db).close()

    def store_fn(db_path, content, type, tags, source):
        return knowledge.store(
            db_path, content, type=type, tags=tags, confidence="high", source=source
        )

    def _seed_sourced_entries(db_path):
        # No source filter on list_entries — enumerate everything (a large
        # limit stands in for "unbounded"; seeded corpora are small local
        # knowledge files) and narrow to seed-sourced rows here.
        # include_superseded=True so superseded rows (excluded by default)
        # are included too — otherwise stale seed-sourced rows would
        # survive a purge or a ground-truth convergence sweep instead of
        # being cleared with everything else.
        return [
            entry
            for entry in knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True)
            if (entry.get("source") or "").startswith("seed:")
        ]

    def _ids_by_source(db_path, source):
        # Reuses the purge helper's enumeration above, narrowed to an exact
        # source match — wired in as sync()'s list_ids_fn so the
        # ground-truth convergence path can find entries a SIGKILL left
        # behind between a store_fn() call and the state-row commit.
        return [
            entry["id"] for entry in _seed_sourced_entries(db_path) if entry.get("source") == source
        ]

    def purge_fn(db_path):
        for entry in _seed_sourced_entries(db_path):
            knowledge.delete(db_path, entry["id"])

    def _ids_by_intake_source(db_path, source):
        # Mirrors _ids_by_source above, wired in as ingest_intake()'s
        # list_ids_fn instead of sync()'s. An exact match against a
        # sha-scoped "intake:<filename>#<sha12>" source can never collide
        # with a "seed:"-sourced knowledge entry, so no prefix filter is
        # needed here the way _seed_sourced_entries needs one (that helper
        # is shared with purge_fn, which does need the broader sweep).
        return [
            entry["id"]
            for entry in knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True)
            if entry.get("source") == source
        ]

    result = sync(
        knowledge_dir,
        techne_db,
        home / "seed_state.db",
        store_fn,
        knowledge.delete,
        purge_fn=purge_fn,
        list_ids_fn=_ids_by_source,
    )
    if not knowledge_dir.is_dir() and result == {"seeded": 0, "removed": 0, "unchanged": 0}:
        # Truly never-seeded bundle: no knowledge/ now, and no prior state to
        # purge either — a clean no-op, so keep the quieter fast-path log.
        log.info("no knowledge/ in bundle — nothing to seed")
    else:
        log.info("seeded=%(seeded)s removed=%(removed)s unchanged=%(unchanged)s", result)

    intake_dir = bundle / "intake"
    if intake_dir.is_dir():
        intake_result = ingest_intake(
            intake_dir,
            techne_db,
            home / "seed_state.db",
            store_fn,
            knowledge.delete,
            list_ids_fn=_ids_by_intake_source,
        )
        log.info("intake: ingested=%(ingested)s skipped=%(skipped)s", intake_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
