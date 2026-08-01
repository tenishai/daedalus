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


def _state_conn(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seed_state (
               filename TEXT PRIMARY KEY,
               sha256 TEXT NOT NULL,
               entry_ids TEXT NOT NULL
           )"""
    )
    return conn


def sync(knowledge_dir: Path, techne_db: Path, state_db: Path, store_fn, delete_fn) -> dict:
    conn = _state_conn(state_db)
    try:
        state = {
            row[0]: {"sha256": row[1], "entry_ids": json.loads(row[2])}
            for row in conn.execute("SELECT filename, sha256, entry_ids FROM seed_state")
        }
        current = {p.name: p for p in sorted(knowledge_dir.glob("*.md"))}
        seeded = removed = unchanged = 0

        for filename in set(state) - set(current):
            for entry_id in state[filename]["entry_ids"]:
                delete_fn(techne_db, entry_id)
            conn.execute("DELETE FROM seed_state WHERE filename = ?", (filename,))
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
            ids = [
                store_fn(techne_db, section, type_for(filename), "seed", f"seed:{filename}")
                for section in parse_knowledge_file(path)
            ]
            conn.execute(
                "INSERT OR REPLACE INTO seed_state (filename, sha256, entry_ids) VALUES (?, ?, ?)",
                (filename, digest, json.dumps(ids)),
            )
            seeded += len(ids)
        conn.commit()
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

    result = sync(knowledge_dir, techne_db, home / "seed_state.db", store_fn, knowledge.delete)
    log.info("seeded=%(seeded)s removed=%(removed)s unchanged=%(unchanged)s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
