"""SQLite-backed checkpoint store — durable execution for the tool loop.

A checkpoint is written before every LLM call and dropped on completion;
failed tasks keep theirs so they can be inspected or resumed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class SqliteCheckpoints:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints ("
                "task_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    async def save(self, task_id: str, state: dict) -> None:
        payload = json.dumps(state, default=str)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (task_id, state, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "state = excluded.state, updated_at = CURRENT_TIMESTAMP",
                (task_id, payload),
            )

    async def load(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def drop(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM checkpoints WHERE task_id = ?", (task_id,))
