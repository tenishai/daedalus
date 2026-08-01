"""Convergent knowledge seeder for the daedalus runtime image.

Seeds bundle/knowledge/*.md into a techne SQLite store at boot. Convergent
per file: only files whose content hash changed are re-seeded; entries from
removed files are dropped; learned (non-seed) entries are never touched.

Section convention: knowledge files are split on `## ` headings; the filename
stem picks the techne knowledge type (see TYPE_MAP; anything else is a fact).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
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
        return []
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


def _state_conn(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_SEED_STATE_SCHEMA)
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

            # TODO(P1): a SIGKILL landing between a store_fn() call and the
            # state-row commit below orphans that seed entry — no exception
            # is raised, so the except-block cleanup never runs, and no
            # state row is written to remember it either. Before PVC-backed
            # memory lands, make file seeding convergent from techne ground
            # truth instead: on startup, purge `seed:<filename>`-sourced
            # entries for any filename that has no corresponding state row.
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


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="seeder: %(message)s")
    if len(argv) != 2:
        log.error("usage: seeder.py <bundle_dir>")
        return 2
    knowledge_dir = Path(argv[1]) / "knowledge"
    if not knowledge_dir.is_dir():
        log.info("no knowledge/ in bundle — nothing to seed")
        return 0
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

    def purge_fn(db_path):
        # No source filter on list_entries — enumerate everything (a large
        # limit stands in for "unbounded"; seeded corpora are small local
        # knowledge files) and drop only the seed-sourced entries.
        # include_superseded=True so superseded rows (excluded by default)
        # are purged too — otherwise stale seed-sourced rows would survive
        # a rebuild instead of being cleared with everything else.
        for entry in knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True):
            if (entry.get("source") or "").startswith("seed:"):
                knowledge.delete(db_path, entry["id"])

    result = sync(
        knowledge_dir,
        techne_db,
        home / "seed_state.db",
        store_fn,
        knowledge.delete,
        purge_fn=purge_fn,
    )
    log.info("seeded=%(seeded)s removed=%(removed)s unchanged=%(unchanged)s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
