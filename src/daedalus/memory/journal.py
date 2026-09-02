"""Conversation journal: the durable record the daily-learnings summarizer
reads. Checkpoints cannot serve this purpose -- a task's checkpoint row is
dropped the moment the task completes (agent.py, _loop), and checkpoints
carry neither timestamps in their payload nor a list API. This journal is
the opposite trade: one row per context_id, upserted AT completion, kept
for RETENTION_DAYS, pruned on every write.

It lives in journal.db beside the techne db on the agent's persistent
volume (see journal_path), NOT in ~/.cache -- the journal must survive pod
restarts to answer "what happened yesterday".

Best-effort by contract: the one runtime writer (Agent._journal_conversation)
wraps record() in try/except -- a journal failure must never fail the task
that produced the conversation.

Timestamps are ISO-8601 UTC strings; every stamp is written with the same
offset, so lexicographic comparison in SQL is chronologically correct.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

JOURNAL_FILENAME = "journal.db"
RETENTION_DAYS = 7

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS conversations ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "context_id TEXT NOT NULL, "
    "history TEXT NOT NULL, "
    "created_at TEXT NOT NULL)"
)


def journal_path(memory_db_path: str | Path) -> Path:
    """The journal lives beside the techne db, on the same volume."""
    return Path(memory_db_path).parent / JOURNAL_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_created_at "
        "ON conversations(created_at)"
    )
    return conn


def record(
    path: str | Path,
    context_id: str,
    history: list[dict],
    *,
    now: datetime | None = None,
) -> None:
    """Record one completed conversation, upserted per context_id.
    `history` is a list of {"role": ..., "text": ...} dicts
    (Message.model_dump() shape). Slack keeps one Task per thread, so
    Task.history accumulates across turns -- each write's history is a
    superset of the last one recorded under the same context_id. Without
    the upsert an N-turn thread would leave N rows, each holding a bigger
    copy of the same conversation (quadratic growth); the prior row for
    this context_id is deleted before the new one is inserted, in the same
    transaction, so exactly one row per context_id ever survives. Rows
    older than RETENTION_DAYS are pruned in the same write, so the journal
    is self-limiting without any separate sweeper."""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=RETENTION_DAYS)).isoformat()
    conn = _connect(Path(path))
    try:
        conn.execute("DELETE FROM conversations WHERE context_id = ?", (context_id,))
        conn.execute(
            "INSERT INTO conversations (context_id, history, created_at) "
            "VALUES (?, ?, ?)",
            (context_id, json.dumps(history), moment.isoformat()),
        )
        conn.execute("DELETE FROM conversations WHERE created_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def recent(
    path: str | Path, *, hours: int = 24, now: datetime | None = None
) -> list[dict]:
    """Conversations from the last `hours`, oldest first. A missing journal
    file reads as no conversations, never an error; a row whose history
    fails to parse is skipped rather than sinking the read."""
    journal = Path(path)
    if not journal.exists():
        return []
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(hours=hours)).isoformat()
    conn = _connect(journal)
    try:
        rows = conn.execute(
            "SELECT context_id, history, created_at FROM conversations "
            "WHERE created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for context_id, history, created_at in rows:
        try:
            parsed = json.loads(history)
        except json.JSONDecodeError:
            continue
        out.append(
            {"context_id": context_id, "history": parsed, "created_at": created_at}
        )
    return out
